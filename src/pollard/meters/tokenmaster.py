"""Optional tokenmaster-backed token and USD meters."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal
from importlib import import_module
from typing import TYPE_CHECKING, Any

from . import DepthMeter, Meter, MeterPrecheckRefusal, StepMeter, WallClockMeter

if TYPE_CHECKING:
    from . import Estimator


class TokenmasterMeter:
    """Record Pollard model-call usage with tokenmaster.

    The returned charge remains a per-call token volume, so this meter can
    replace ``TokenMeter`` in Pollard budgets. The richer tokenmaster gauge,
    advice, and optional request-limit diagnostics are merged under
    ``meta["tokenmaster"]`` on each charged node.

    Profile-limit enforcement is opt-in because a Pollard token budget is a
    cumulative run budget, while a model profile is a per-request capacity.
    Enabling it requires an estimator so the request can be checked before
    provider dispatch.
    """

    name = "tokens"

    def __init__(
        self,
        model: str | None = None,
        *,
        meter: Any | None = None,
        estimator: Estimator | None = None,
        reserved_output: int = 0,
        expected_remaining_turns: int | None = None,
        task: Any | None = None,
        policy: Any | None = None,
        enforce_profile_limits: bool = False,
        profile_capacity: str = "nominal",
    ) -> None:
        if meter is not None and model is not None:
            raise ValueError("pass either model or meter, not both")
        if expected_remaining_turns is not None and task is not None:
            raise ValueError("pass either expected_remaining_turns or task, not both")
        _validate_reserved_output(reserved_output)
        if not isinstance(enforce_profile_limits, bool):
            raise ValueError("enforce_profile_limits must be a bool")
        if profile_capacity not in {"nominal", "effective"}:
            raise ValueError("profile_capacity must be 'nominal' or 'effective'")
        if enforce_profile_limits and estimator is None:
            raise ValueError("profile-limit enforcement requires an estimator")

        self._model = model
        self._meter = meter
        self._profile_meters: dict[str, Any] = {}
        self._estimator = estimator
        self._reserved_output = reserved_output
        self._expected_remaining_turns = expected_remaining_turns
        self._task = task
        self._policy = policy
        self._enforce_profile_limits = enforce_profile_limits
        self._profile_capacity = profile_capacity
        self._warned_missing_usage = False
        self._warned_missing_model = False
        self._warned_settlement_error = False
        self.precheck_is_estimate = estimator is not None

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        if node_kind != "model_call" or result is None:
            return 0
        if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
            self._warn_missing_usage_once()
            return 0

        usage = result["usage"]
        target = self._settlement_target(payload, result, usage)
        turn_payload = _exclusive_turn_payload(result)
        charge = _context_total(turn_payload)
        limits = (
            self._settlement_limits(target, turn_payload)
            if self._enforce_profile_limits
            else None
        )

        try:
            meter = self._ensure_meter(target)
            if meter is None:
                self._warn_missing_model_once()
                if limits is not None:
                    _tokenmaster_meta(meta)["limits"] = limits
                return charge
            profile_model = _profile_model(meter)
            if profile_model is not None:
                turn_payload["model_id"] = profile_model
            turn = meter.record(turn_payload)
            state = meter.state()
            task = self._ensure_task()
            advice = meter.advise(task=task, policy=self._policy)
        except Exception as exc:  # settlement must not mask a completed provider call
            self._warn_settlement_error_once(exc)
            tokenmaster_meta = _tokenmaster_meta(meta)
            if limits is not None:
                tokenmaster_meta["limits"] = limits
            tokenmaster_meta["meter"] = {
                "status": "error",
                "error": _safe_error(exc),
            }
            return charge

        tokenmaster_meta = _tokenmaster_meta(meta)
        if limits is not None:
            tokenmaster_meta["limits"] = limits
        tokenmaster_meta.update(
            {
                "turn": turn.to_dict(),
                "state": state.to_dict(),
                "advice": advice.to_dict(),
            }
        )
        if task is not None and hasattr(task, "to_dict"):
            tokenmaster_meta["task"] = task.to_dict()
        return int(turn.context_total())

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, Any],
    ) -> int | None:
        if node_kind != "model_call" or self._estimator is None:
            return None
        estimate = _estimate_input(self._estimator, payload)
        if estimate is None:
            if self._enforce_profile_limits:
                self._refuse_unavailable(
                    payload,
                    code="missing_input_estimate",
                    detail="tokenmaster profile enforcement needs an input-token estimate",
                )
            return None

        if not self._enforce_profile_limits:
            return estimate + self._reserved_output

        requested_output = _requested_output_tokens(payload)
        target = self._preflight_target(payload)
        if target is None:
            self._refuse_unavailable(
                payload,
                code="missing_model",
                detail="tokenmaster profile enforcement needs a model id",
            )
        tokenmaster = _load_tokenmaster()
        try:
            check = tokenmaster.check_request_limits(
                target,
                input_tokens=estimate,
                requested_output_tokens=requested_output,
                reserved_output_tokens=self._reserved_output,
                capacity=self._profile_capacity,
            )
        except Exception as exc:
            model_id = _target_model_id(target)
            raise MeterPrecheckRefusal(
                "tokenmaster_profile_unavailable",
                f"tokenmaster could not resolve request limits for {model_id or 'the model'}",
                audit_meta={
                    "meter": self.name,
                    "tokenmaster": {
                        **({"model_id": model_id} if model_id is not None else {}),
                        "limits": {
                            "status": "unavailable",
                            "reason": "profile_lookup_failed",
                            "error": _safe_error(exc),
                        },
                    },
                },
            ) from exc
        if not check.allowed:
            requested, remaining = _limit_requested_remaining(check)
            raise MeterPrecheckRefusal(
                "tokenmaster_profile_limit",
                "tokenmaster model profile refused the request: "
                + ", ".join(check.violations),
                audit_meta={
                    "meter": self.name,
                    "tokenmaster": {
                        "model_id": check.model_id,
                        "limits": check.to_dict(),
                    },
                },
                requested=requested,
                remaining=remaining,
            )
        return estimate + int(check.context_output_tokens)

    def _preflight_target(self, payload: dict[str, Any]) -> Any | None:
        bound = self._bound_target()
        if bound is not None:
            return bound
        value = payload.get("model")
        return value if isinstance(value, str) and value else None

    def _settlement_target(
        self,
        payload: dict[str, Any],
        result: dict[str, Any],
        usage: dict[str, Any],
    ) -> Any | None:
        bound = self._bound_target()
        if bound is not None:
            return bound
        for value in (result.get("model"), usage.get("model_id"), payload.get("model")):
            if isinstance(value, str) and value:
                return value
        return None

    def _bound_target(self) -> Any | None:
        if self._meter is not None:
            profile = getattr(self._meter, "profile", None)
            if profile is not None:
                return profile
        return self._model

    def _ensure_meter(self, target: Any | None) -> Any | None:
        if self._meter is not None:
            return self._meter
        if target is None:
            return None
        tokenmaster = _load_tokenmaster()
        profile = tokenmaster.get_profile(target) if isinstance(target, str) else target
        model_id = _target_model_id(profile)
        if model_id is not None and model_id in self._profile_meters:
            return self._profile_meters[model_id]
        resolved = tokenmaster.Meter(
            profile,
            reserved_output=self._reserved_output,
        )
        if model_id is not None:
            self._profile_meters[model_id] = resolved
        return resolved

    def _ensure_task(self) -> Any | None:
        if self._task is not None:
            return self._task
        if self._expected_remaining_turns is None:
            return None
        tokenmaster = _load_tokenmaster()
        self._task = tokenmaster.TaskContext(
            expected_remaining_turns=self._expected_remaining_turns
        )
        return self._task

    def _settlement_limits(
        self,
        target: Any | None,
        turn_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if target is None:
            return {"status": "unavailable", "reason": "missing_model"}
        request_input = sum(
            int(turn_payload.get(key, 0))
            for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
        )
        observed_output = sum(
            int(turn_payload.get(key, 0))
            for key in ("output_tokens", "reasoning_tokens")
        )
        try:
            tokenmaster = _load_tokenmaster()
            check = tokenmaster.check_request_limits(
                target,
                input_tokens=request_input,
                requested_output_tokens=observed_output,
                reserved_output_tokens=self._reserved_output,
                capacity=self._profile_capacity,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": "profile_lookup_failed",
                "error": _safe_error(exc),
            }
        result = dict(check.to_dict())
        result["phase"] = "settlement"
        return result

    def _refuse_unavailable(
        self,
        payload: dict[str, Any],
        *,
        code: str,
        detail: str,
    ) -> None:
        model_id = _target_model_id(self._preflight_target(payload))
        raise MeterPrecheckRefusal(
            "tokenmaster_profile_unavailable",
            detail,
            audit_meta={
                "meter": self.name,
                "tokenmaster": {
                    **({"model_id": model_id} if model_id is not None else {}),
                    "limits": {"status": "unavailable", "reason": code},
                },
            },
        )

    def _warn_missing_usage_once(self) -> None:
        if self._warned_missing_usage:
            return
        self._warned_missing_usage = True
        _warn_safely("pollard tokenmaster meter saw no compatible usage payload")

    def _warn_missing_model_once(self) -> None:
        if self._warned_missing_model:
            return
        self._warned_missing_model = True
        _warn_safely("pollard tokenmaster meter needs a model id or tokenmaster Meter")

    def _warn_settlement_error_once(self, exc: Exception) -> None:
        if self._warned_settlement_error:
            return
        self._warned_settlement_error = True
        _warn_safely(
            f"pollard tokenmaster meter could not record completed usage: {_safe_error(exc)}"
        )


class TokenmasterCostMeter:
    """Tier-aware USD governance backed by tokenmaster 0.2 pricing profiles.

    Preflight uses tokenmaster's conservative request quote, while settlement
    uses the provider's exclusive usage categories. Missing, incomplete, or
    non-USD pricing fails closed before dispatch. A completed provider call is
    never replaced by a settlement exception.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        meter: Any | None = None,
        estimator: Estimator,
        reserved_output: int = 0,
        name: str = "usd",
    ) -> None:
        if meter is not None and model is not None:
            raise ValueError("pass either model or meter, not both")
        if not isinstance(name, str) or not name:
            raise ValueError("cost meter name must be a non-empty string")
        _validate_reserved_output(reserved_output)
        self.name = name
        self._model = model
        self._meter = meter
        self._estimator = estimator
        self._reserved_output = reserved_output
        self._warned_missing_usage = False
        self.precheck_is_estimate = True

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, Any],
    ) -> Decimal | None:
        if node_kind != "model_call":
            return None
        estimate = _estimate_input(self._estimator, payload)
        if estimate is None:
            self._refuse_pricing(
                payload,
                reason="missing_input_estimate",
                detail="tokenmaster USD governance needs an input-token estimate",
            )
        target = self._preflight_target(payload)
        if target is None:
            self._refuse_pricing(
                payload,
                reason="missing_model",
                detail="tokenmaster USD governance needs a model id",
            )
        requested_output = _requested_output_tokens(payload)
        reserved_output = max(self._reserved_output, requested_output or 0)
        tokenmaster = _load_tokenmaster()
        try:
            quote = tokenmaster.quote_estimate(
                target,
                input_tokens=estimate,
                reserved_output_tokens=reserved_output,
                conservative=True,
            )
        except Exception as exc:
            self._refuse_pricing(
                payload,
                target=target,
                reason="pricing_lookup_failed",
                detail="tokenmaster could not conservatively price the request",
                error=exc,
            )
        if quote.currency != "USD":
            model_id = quote.model_id
            raise MeterPrecheckRefusal(
                "tokenmaster_currency",
                f"tokenmaster USD governance does not support {quote.currency} pricing",
                audit_meta={
                    "meter": self.name,
                    "tokenmaster": {
                        "model_id": model_id,
                        "pricing": {
                            "status": "unsupported_currency",
                            "currency": quote.currency,
                        },
                    },
                },
            )
        return _decimal_estimate_total(quote)

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> Decimal:
        if node_kind != "model_call" or result is None:
            return Decimal("0")
        if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
            self._warn_missing_usage_once()
            return Decimal("0")

        usage = result["usage"]
        target = self._settlement_target(payload, result, usage)
        tokenmaster_meta = _tokenmaster_meta(meta)
        if target is None:
            tokenmaster_meta["cost"] = {
                "status": "unavailable",
                "reason": "missing_model",
            }
            return Decimal("0")
        try:
            turn_payload = _exclusive_turn_payload(result)
            tokenmaster = _load_tokenmaster()
            turn = tokenmaster.TurnUsage(turn_id=0, **turn_payload)
            quote = tokenmaster.quote_usage(target, turn)
            if quote.currency != "USD":
                raise ValueError(f"unsupported pricing currency {quote.currency!r}")
            amount = _decimal_quote_total(quote, turn_payload)
        except Exception as exc:  # settlement must not mask a completed provider call
            tokenmaster_meta["cost"] = {
                "status": "unavailable",
                "reason": "pricing_failed",
                "error": _safe_error(exc),
            }
            return Decimal("0")

        cost_meta = quote.to_dict()
        cost_meta.update(
            {
                "status": "quoted",
                "total_cost_decimal": str(amount),
            }
        )
        tokenmaster_meta["cost"] = cost_meta
        return amount

    def precheck_fallback_reason(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> str | None:
        """Request conservative settlement when exact postflight pricing failed."""

        del payload, result
        if node_kind != "model_call":
            return None
        tokenmaster_meta = meta.get("tokenmaster")
        if not isinstance(tokenmaster_meta, Mapping):
            return None
        cost = tokenmaster_meta.get("cost")
        if isinstance(cost, Mapping) and cost.get("status") == "unavailable":
            return "exact_pricing_unavailable"
        return None

    def _bound_target(self) -> Any | None:
        if self._meter is not None:
            profile = getattr(self._meter, "profile", None)
            if profile is not None:
                return profile
        return self._model

    def _preflight_target(self, payload: dict[str, Any]) -> Any | None:
        bound = self._bound_target()
        if bound is not None:
            return bound
        value = payload.get("model")
        return value if isinstance(value, str) and value else None

    def _settlement_target(
        self,
        payload: dict[str, Any],
        result: dict[str, Any],
        usage: dict[str, Any],
    ) -> Any | None:
        bound = self._bound_target()
        if bound is not None:
            return bound
        for value in (result.get("model"), usage.get("model_id"), payload.get("model")):
            if isinstance(value, str) and value:
                return value
        return None

    def _refuse_pricing(
        self,
        payload: dict[str, Any],
        *,
        reason: str,
        detail: str,
        target: Any | None = None,
        error: Exception | None = None,
    ) -> None:
        resolved = self._preflight_target(payload) if target is None else target
        model_id = _target_model_id(resolved)
        pricing: dict[str, Any] = {"status": "unavailable", "reason": reason}
        if error is not None:
            pricing["error"] = _safe_error(error)
        raise MeterPrecheckRefusal(
            "tokenmaster_pricing_unavailable",
            detail,
            audit_meta={
                "meter": self.name,
                "tokenmaster": {
                    **({"model_id": model_id} if model_id is not None else {}),
                    "pricing": pricing,
                },
            },
        )

    def _warn_missing_usage_once(self) -> None:
        if self._warned_missing_usage:
            return
        self._warned_missing_usage = True
        _warn_safely("pollard tokenmaster cost meter saw no compatible usage payload")


def tokenmaster_governance_meters(
    model: str | None = None,
    *,
    estimator: Estimator,
    reserved_output: int = 0,
    expected_remaining_turns: int | None = None,
    task: Any | None = None,
    policy: Any | None = None,
    enforce_profile_limits: bool = False,
    profile_capacity: str = "nominal",
    cost_name: str = "usd",
) -> list[Meter]:
    """Build the standard Runtime meter set with explicit Tokenmaster governance.

    This factory replaces only the built-in ``TokenMeter``. It retains step,
    depth, and wall-clock accounting, then adds Tokenmaster token and USD
    meters that share the caller's model binding, estimator, and output
    reservation. Passing the returned list to ``Runtime(meters=...)`` or
    ``AsyncRuntime(meters=...)`` is always explicit; Runtime defaults remain
    backward compatible.
    """

    return [
        StepMeter(),
        DepthMeter(),
        WallClockMeter(),
        TokenmasterMeter(
            model,
            estimator=estimator,
            reserved_output=reserved_output,
            expected_remaining_turns=expected_remaining_turns,
            task=task,
            policy=policy,
            enforce_profile_limits=enforce_profile_limits,
            profile_capacity=profile_capacity,
        ),
        TokenmasterCostMeter(
            model,
            estimator=estimator,
            reserved_output=reserved_output,
            name=cost_name,
        ),
    ]


def _load_tokenmaster() -> Any:
    try:
        return import_module("tokenmaster")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Tokenmaster meters require the tokenmaster extra; install pollard[tokenmaster]"
        ) from exc


def _exclusive_turn_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Build Tokenmaster's exclusive usage categories without double counting."""
    normalized = result.get("usage")
    if not isinstance(normalized, Mapping):
        return _empty_turn_payload()
    raw = result.get("provider_usage")
    if not isinstance(raw, Mapping):
        return _canonical_turn_payload(normalized)

    if _uses_separate_cache_fields(raw):
        return {
            "input_tokens": _int_usage(raw, "input_tokens", "inputTokens", "prompt_tokens"),
            "cache_read_tokens": _int_usage(
                raw,
                "cache_read_tokens",
                "cache_read_input_tokens",
                "cacheReadInputTokens",
            ),
            "cache_write_tokens": _int_usage(
                raw,
                "cache_write_tokens",
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
                "cacheWriteInputTokens",
            ),
            "output_tokens": _int_usage(
                raw,
                "output_tokens",
                "outputTokens",
                "completion_tokens",
            ),
            "reasoning_tokens": _int_usage(raw, "reasoning_tokens"),
        }

    input_total = _int_usage(raw, "input_tokens", "prompt_tokens")
    if not _has_int_usage(raw, "input_tokens", "prompt_tokens"):
        input_total = _int_usage(normalized, "input_tokens", "prompt_tokens")
    output_total = _int_usage(raw, "output_tokens", "completion_tokens")
    if not _has_int_usage(raw, "output_tokens", "completion_tokens"):
        output_total = _int_usage(normalized, "output_tokens", "completion_tokens")

    input_details = _details(raw, "input_tokens_details", "prompt_tokens_details")
    output_details = _details(raw, "output_tokens_details", "completion_tokens_details")
    cache_read = min(
        input_total,
        _int_usage(input_details, "cached_tokens")
        or _int_usage(raw, "cached_input_tokens"),
    )
    cache_write = min(
        input_total - cache_read,
        _int_usage(input_details, "cache_write_tokens")
        or _int_usage(raw, "cache_write_tokens", "cache_creation_input_tokens"),
    )
    reasoning = min(
        output_total,
        _int_usage(output_details, "reasoning_tokens")
        or _int_usage(raw, "reasoning_tokens"),
    )
    return {
        "input_tokens": input_total - cache_read - cache_write,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": output_total - reasoning,
        "reasoning_tokens": reasoning,
    }


def _canonical_turn_payload(usage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": _int_usage(usage, "input_tokens", "prompt_tokens"),
        "cache_read_tokens": _int_usage(
            usage,
            "cache_read_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
        ),
        "cache_write_tokens": _int_usage(
            usage,
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
        ),
        "output_tokens": _int_usage(usage, "output_tokens", "completion_tokens"),
        "reasoning_tokens": _int_usage(usage, "reasoning_tokens"),
    }


def _empty_turn_payload() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }


def _uses_separate_cache_fields(usage: Mapping[str, Any]) -> bool:
    return any(
        key in usage
        for key in (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
            "cacheWriteInputTokens",
        )
    )


def _details(usage: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _context_total(turn: Mapping[str, Any]) -> int:
    return sum(
        int(turn.get(name, 0))
        for name in (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_tokens",
        )
    )


def _int_usage(usage: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _has_int_usage(usage: Mapping[str, Any], *keys: str) -> bool:
    return any(
        isinstance(usage.get(key), int)
        and not isinstance(usage.get(key), bool)
        and int(usage[key]) >= 0
        for key in keys
    )


def _requested_output_tokens(payload: Mapping[str, Any]) -> int | None:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative int")
        return int(value)
    return None


def _estimate_input(estimator: Any, payload: dict[str, Any]) -> int | None:
    estimate = estimator.estimate_input_tokens(payload)
    if estimate is None:
        return None
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        raise ValueError("token estimator must return a non-negative int or None")
    return int(estimate)


def _limit_requested_remaining(check: Any) -> tuple[int, int]:
    if check.input_exceeded:
        return int(check.input_tokens), int(check.max_input_tokens)
    if check.context_exceeded:
        return int(check.context_tokens), int(check.capacity)
    if check.output_exceeded:
        return int(check.requested_output_tokens), int(check.max_output_tokens)
    raise AssertionError("a refused limit check must have a violation")


def _validate_reserved_output(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("reserved_output must be a non-negative int")


def _tokenmaster_meta(meta: dict[str, Any]) -> dict[str, Any]:
    value = meta.get("tokenmaster")
    if isinstance(value, dict):
        return value
    value = {}
    meta["tokenmaster"] = value
    return value


def _profile_model(meter: Any) -> str | None:
    profile = getattr(meter, "profile", None)
    value = getattr(profile, "model_id", None)
    return value if isinstance(value, str) and value else None


def _target_model_id(target: Any | None) -> str | None:
    if isinstance(target, str):
        return target
    value = getattr(target, "model_id", None)
    return value if isinstance(value, str) and value else None


def _decimal_quote_total(quote: Any, turn: Mapping[str, Any]) -> Decimal:
    pricing = quote.pricing
    million = Decimal(1_000_000)
    return (
        Decimal(int(turn["input_tokens"])) * Decimal(str(pricing.input))
        + Decimal(int(turn["cache_read_tokens"])) * Decimal(str(pricing.cache_read))
        + Decimal(int(turn["cache_write_tokens"])) * Decimal(str(pricing.cache_write))
        + Decimal(int(turn["output_tokens"])) * Decimal(str(pricing.output))
        + Decimal(int(turn["reasoning_tokens"])) * Decimal(str(pricing.output))
    ) / million


def _decimal_estimate_total(quote: Any) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(quote.input_tokens) * Decimal(str(quote.input_rate))
        + Decimal(quote.reserved_output_tokens) * Decimal(str(quote.output_rate))
    ) / million


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _warn_safely(message: str) -> None:
    """Emit a best-effort diagnostic without masking completed provider work."""

    # Warning filters may promote warnings to exceptions, and custom
    # showwarning hooks may also fail. Settlement must remain non-raising.
    with suppress(Exception):
        warnings.warn(message, stacklevel=3)
