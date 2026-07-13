"""Optional tokenmaster-backed token meter."""

from __future__ import annotations

import warnings
from importlib import import_module
from typing import Any


class TokenmasterMeter:
    """Record Pollard model-call usage with tokenmaster.

    The returned charge remains a per-call token volume, so it can replace
    ``TokenMeter`` in Pollard budgets. The richer tokenmaster gauge and advice
    are stored under ``meta["tokenmaster"]`` on each charged node.
    """

    name = "tokens"

    def __init__(
        self,
        model: str | None = None,
        *,
        meter: Any | None = None,
        reserved_output: int = 0,
        expected_remaining_turns: int | None = None,
        task: Any | None = None,
        policy: Any | None = None,
    ) -> None:
        if meter is not None and model is not None:
            raise ValueError("pass either model or meter, not both")
        if expected_remaining_turns is not None and task is not None:
            raise ValueError("pass either expected_remaining_turns or task, not both")
        self._model = model
        self._meter = meter
        self._reserved_output = reserved_output
        self._expected_remaining_turns = expected_remaining_turns
        self._task = task
        self._policy = policy
        self._warned_missing_usage = False
        self._warned_missing_model = False

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        if node_kind != "model_call":
            return 0
        if result is None:
            return 0
        if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
            self._warn_missing_usage_once()
            return 0

        usage = result["usage"]
        model_id = self._usage_model(payload, usage)
        turn_payload = _turn_payload(usage, model_id)
        charge = _context_total(turn_payload)
        meter = self._ensure_meter(model_id)
        if meter is None:
            self._warn_missing_model_once()
            return charge

        turn = meter.record(turn_payload)
        state = meter.state()
        task = self._ensure_task()
        advice = meter.advise(task=task, policy=self._policy)

        tokenmaster_meta: dict[str, Any] = {
            "turn": turn.to_dict(),
            "state": state.to_dict(),
            "advice": advice.to_dict(),
        }
        if task is not None and hasattr(task, "to_dict"):
            tokenmaster_meta["task"] = task.to_dict()
        meta["tokenmaster"] = tokenmaster_meta
        return int(turn.context_total())

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> None:
        del node_kind, payload
        return None

    def _ensure_meter(self, model_id: str | None) -> Any | None:
        if self._meter is not None:
            return self._meter
        chosen_model = self._model or model_id
        if chosen_model is None:
            return None
        tokenmaster = _load_tokenmaster()
        self._meter = tokenmaster.Meter.for_model(
            chosen_model,
            reserved_output=self._reserved_output,
        )
        return self._meter

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

    def _usage_model(self, payload: dict[str, Any], usage: dict[str, Any]) -> str | None:
        value = usage.get("model_id")
        if isinstance(value, str):
            return value
        value = payload.get("model")
        if isinstance(value, str):
            return value
        if self._meter is not None and hasattr(self._meter, "profile"):
            profile = self._meter.profile
            profile_model = getattr(profile, "model_id", None)
            if isinstance(profile_model, str):
                return profile_model
        return self._model

    def _warn_missing_usage_once(self) -> None:
        if self._warned_missing_usage:
            return
        self._warned_missing_usage = True
        warnings.warn("pollard tokenmaster meter saw no compatible usage payload", stacklevel=2)

    def _warn_missing_model_once(self) -> None:
        if self._warned_missing_model:
            return
        self._warned_missing_model = True
        warnings.warn(
            "pollard tokenmaster meter needs a model id or tokenmaster Meter",
            stacklevel=2,
        )


def _load_tokenmaster() -> Any:
    try:
        return import_module("tokenmaster")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "TokenmasterMeter requires the tokenmaster extra; install pollard[tokenmaster]"
        ) from exc


def _turn_payload(usage: dict[str, Any], model_id: str | None) -> dict[str, Any]:
    turn: dict[str, Any] = {
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
    if model_id is not None:
        turn["model_id"] = model_id
    return turn


def _context_total(turn: dict[str, Any]) -> int:
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


def _int_usage(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0
