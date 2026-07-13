import warnings
from decimal import Decimal

import pytest

from pollard.meters import (
    CostMeter,
    StepMeter,
    TokenMeter,
    WallClockMeter,
    WindowMeter,
    usage_from_anthropic,
    usage_from_openai,
)


def test_step_meter_charges_model_and_tool_calls_only() -> None:
    meter = StepMeter()
    assert meter.precheck_estimate("model_call", {}) == 1
    assert meter.charge("tool_call", {}, {}, {}) == 1
    assert meter.charge("note", {}, {}, {}) == 0


def test_wall_clock_meter_reads_duration_meta() -> None:
    meter = WallClockMeter()
    assert meter.charge("model_call", {}, {}, {"duration_s": 0.25}) == 0.25
    assert meter.charge("model_call", {}, {}, {}) == 0.0


def test_token_meter_reads_standard_usage_shape() -> None:
    meter = TokenMeter()
    assert (
        meter.charge(
            "model_call",
            {},
            {"usage": {"input_tokens": 10, "output_tokens": 3}},
            {},
        )
        == 13
    )


def test_token_meter_warns_once_for_missing_usage() -> None:
    meter = TokenMeter()
    with pytest.warns(UserWarning):
        assert meter.charge("model_call", {}, {"text": "ok"}, {}) == 0
    with warnings.catch_warnings(record=True) as caught:
        assert meter.charge("model_call", {}, {"text": "ok"}, {}) == 0
    assert caught == []


def test_token_meter_uses_input_estimator_and_output_reservation() -> None:
    class FixedEstimator:
        def estimate_input_tokens(self, payload: dict[str, object]) -> int:
            assert payload["model"] == "mock-1"
            return 7

    meter = TokenMeter(FixedEstimator(), reserved_output_tokens=5)
    assert meter.precheck_estimate("model_call", {"model": "mock-1"}) == 12
    assert meter.precheck_estimate("tool_call", {}) is None
    assert meter.precheck_is_estimate is True


def test_token_meter_rejects_invalid_estimates() -> None:
    class BadEstimator:
        def estimate_input_tokens(self, payload: dict[str, object]) -> int:
            del payload
            return -1

    with pytest.raises(ValueError, match="non-negative"):
        TokenMeter(BadEstimator()).precheck_estimate("model_call", {})
    with pytest.raises(ValueError, match="reserved_output_tokens"):
        TokenMeter(reserved_output_tokens=-1)


def test_cost_meter_uses_decimal_arithmetic() -> None:
    meter = CostMeter({"mock-1": {"input_per_1m": "2.00", "output_per_1m": "6.00"}})
    assert meter.charge(
        "model_call",
        {"model": "mock-1"},
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}},
        {},
    ) == Decimal("5.00")


def test_cost_meter_returns_zero_for_missing_price_or_usage() -> None:
    meter = CostMeter({"mock-1": {"input_per_1m": "2.00", "output_per_1m": "6.00"}})
    assert meter.charge("model_call", {"model": "missing"}, {}, {}) == Decimal("0")
    assert meter.charge("model_call", {"model": "mock-1"}, {"usage": {}}, {}) == Decimal("0")


def test_window_meter_supports_request_and_token_windows() -> None:
    requests = WindowMeter("requests", 5, 60)
    assert requests.precheck_estimate("model_call", {}) == 1
    assert requests.charge("tool_call", {}, {}, {}) == 1
    tokens = WindowMeter("tokens", 100, 60)
    assert tokens.charge(
        "model_call",
        {},
        {"usage": {"input_tokens": 7, "output_tokens": 3}},
        {},
    ) == 10
    with pytest.raises(ValueError, match="limit"):
        WindowMeter("requests", 0, 60)
    with pytest.raises(ValueError, match="window_seconds"):
        WindowMeter("requests", 1, 0)


def test_usage_helpers_normalize_provider_shapes() -> None:
    assert usage_from_openai({"usage": {"prompt_tokens": 5, "completion_tokens": 7}}) == {
        "input_tokens": 5,
        "output_tokens": 7,
    }
    assert usage_from_anthropic({"usage": {"input_tokens": 11, "output_tokens": 13}}) == {
        "input_tokens": 11,
        "output_tokens": 13,
    }
    assert usage_from_openai({"usage": "bad"}) == {"input_tokens": 0, "output_tokens": 0}
    assert usage_from_anthropic({}) == {"input_tokens": 0, "output_tokens": 0}
