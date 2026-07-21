"""Built-in meters for pollard budgets."""

from __future__ import annotations

import hashlib
import json
import warnings
from decimal import Decimal
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

if TYPE_CHECKING:
    from .tokenmaster import TokenmasterMeter as TokenmasterMeter

ChargeAmount: TypeAlias = int | float | Decimal


class Meter(Protocol):
    name: str

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> ChargeAmount: ...

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> ChargeAmount | None: ...


class Estimator(Protocol):
    """Approximate the input tokens for a model-call identity payload."""

    def estimate_input_tokens(self, payload: dict[str, Any]) -> int | None: ...


class StepMeter:
    name = "steps"

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        del payload, result, meta
        return 1 if node_kind in {"model_call", "tool_call"} else 0

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> int | None:
        del payload
        return 1 if node_kind in {"model_call", "tool_call"} else 0


class DepthMeter:
    name = "depth"

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        del node_kind, payload, result, meta
        return 0

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> None:
        del node_kind, payload
        return None


class WallClockMeter:
    name = "seconds"

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> float:
        del node_kind, payload, result
        value = meta.get("duration_s", 0.0)
        return float(value) if isinstance(value, int | float) else 0.0

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> None:
        del node_kind, payload
        return None


class TokenMeter:
    name = "tokens"

    def __init__(
        self,
        estimator: Estimator | None = None,
        *,
        reserved_output_tokens: int = 0,
    ) -> None:
        if isinstance(reserved_output_tokens, bool) or reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be a non-negative int")
        self._estimator = estimator
        self._reserved_output_tokens = reserved_output_tokens
        self._warned_missing_usage = False
        self.precheck_is_estimate = estimator is not None

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        del payload, meta
        if node_kind not in {"model_call", "tool_call"}:
            return 0
        if result is None:
            return 0
        if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
            self._warn_missing_usage_once()
            return 0
        usage = result["usage"]
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            self._warn_missing_usage_once()
            return 0
        return int(input_tokens) + int(output_tokens)

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> int | None:
        if node_kind != "model_call" or self._estimator is None:
            return None
        estimate = self._estimator.estimate_input_tokens(payload)
        if estimate is None:
            return None
        if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
            raise ValueError("token estimator must return a non-negative int or None")
        return estimate + self._reserved_output_tokens

    def _warn_missing_usage_once(self) -> None:
        if self._warned_missing_usage:
            return
        self._warned_missing_usage = True
        warnings.warn("pollard token meter saw no compatible usage payload", stacklevel=2)


class CostMeter:
    name = "usd"

    def __init__(self, prices: dict[str, dict[str, ChargeAmount]]) -> None:
        self._prices = {
            model: {
                "input_per_1m": Decimal(str(row["input_per_1m"])),
                "output_per_1m": Decimal(str(row["output_per_1m"])),
            }
            for model, row in prices.items()
        }

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> Decimal:
        del meta
        if node_kind != "model_call" or not isinstance(result, dict):
            return Decimal("0")
        usage = result.get("usage")
        model = payload.get("model")
        if not isinstance(usage, dict) or not isinstance(model, str):
            return Decimal("0")
        price = self._prices.get(model)
        if price is None:
            return Decimal("0")
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            return Decimal("0")
        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * price["input_per_1m"]
            + Decimal(output_tokens) * price["output_per_1m"]
        ) / million

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> None:
        del node_kind, payload
        return None


class WindowMeter:
    """A store-backed sliding-window ceiling around a meter."""

    def __init__(
        self,
        name: str,
        limit: ChargeAmount,
        window_seconds: int | float,
        *,
        meter: Meter | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("window meter name must be a non-empty string")
        if isinstance(limit, bool) or Decimal(str(limit)) <= 0:
            raise ValueError("window meter limit must be positive")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int | float)
            or window_seconds <= 0
        ):
            raise ValueError("window_seconds must be positive")
        self.name = name
        self.limit = Decimal(str(limit))
        self.window_seconds = float(window_seconds)
        if meter is not None:
            self._meter = meter
        elif name == "tokens":
            self._meter = TokenMeter()
        else:
            self._meter = StepMeter()
        self.precheck_is_estimate = getattr(
            self._meter, "precheck_is_estimate", False
        )

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> ChargeAmount:
        return self._meter.charge(node_kind, payload, result, meta)

    def precheck_estimate(
        self, node_kind: str, payload: dict[str, Any]
    ) -> ChargeAmount | None:
        return self._meter.precheck_estimate(node_kind, payload)

    def ledger_key(self, root_id: str) -> str:
        document = json.dumps(
            {
                "root_id": root_id,
                "name": self.name,
                "limit": str(self.limit),
                "window_seconds": str(self.window_seconds),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(document.encode("utf-8")).hexdigest()


def usage_from_openai(resp: dict[str, Any]) -> dict[str, int]:
    usage = resp.get("usage", {})
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": _int_usage(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _int_usage(usage, "output_tokens", "completion_tokens"),
    }


def usage_from_anthropic(resp: dict[str, Any]) -> dict[str, int]:
    usage = resp.get("usage", {})
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": (
            _int_usage(usage, "input_tokens")
            + _int_usage(usage, "cache_creation_input_tokens")
            + _int_usage(usage, "cache_read_input_tokens")
        ),
        "output_tokens": _int_usage(usage, "output_tokens"),
    }


def _int_usage(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def __getattr__(name: str) -> Any:
    if name == "TokenmasterMeter":
        module = import_module("pollard.meters.tokenmaster")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'pollard.meters' has no attribute {name!r}")
