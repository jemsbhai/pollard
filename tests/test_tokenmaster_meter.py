import pytest
from tokenmaster import Meter, ModelProfile

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime
from pollard.meters import StepMeter, TokenmasterMeter


def _tokenmaster_meter() -> Meter:
    return Meter(
        ModelProfile(
            model_id="test:model",
            provider="test",
            window_nominal=1_000,
        )
    )


def test_tokenmaster_meter_charges_usage_and_writes_meta() -> None:
    meter = TokenmasterMeter(
        meter=_tokenmaster_meter(),
        expected_remaining_turns=4,
    )
    meta: dict[str, object] = {}

    charge = meter.charge(
        "model_call",
        {"model": "test:model"},
        {
            "usage": {
                "input_tokens": 10,
                "cache_read_tokens": 3,
                "cache_write_tokens": 4,
                "output_tokens": 5,
                "reasoning_tokens": 2,
            }
        },
        meta,
    )

    assert charge == 24
    tokenmaster = meta["tokenmaster"]
    assert isinstance(tokenmaster, dict)
    assert tokenmaster["turn"]["turn_id"] == 1
    assert tokenmaster["state"]["used_tokens"] == 24
    assert tokenmaster["advice"]["action"] == "continue"
    assert tokenmaster["task"]["expected_remaining_turns"] == 4


def test_tokenmaster_meter_supports_openai_compatible_usage_aliases() -> None:
    meter = TokenmasterMeter(meter=_tokenmaster_meter())
    meta: dict[str, object] = {}

    assert (
        meter.charge(
            "model_call",
            {"model": "test:model"},
            {"usage": {"prompt_tokens": 9, "completion_tokens": 6}},
            meta,
        )
        == 15
    )

    tokenmaster = meta["tokenmaster"]
    assert isinstance(tokenmaster, dict)
    assert tokenmaster["turn"]["input_tokens"] == 9
    assert tokenmaster["turn"]["output_tokens"] == 6


def test_tokenmaster_meter_falls_back_to_charge_when_model_is_missing() -> None:
    meter = TokenmasterMeter()
    meta: dict[str, object] = {}

    with pytest.warns(UserWarning, match="model id"):
        assert (
            meter.charge(
                "model_call",
                {},
                {"usage": {"input_tokens": 5, "output_tokens": 7}},
                meta,
            )
            == 12
        )

    assert "tokenmaster" not in meta


def test_runtime_can_budget_with_tokenmaster_meter() -> None:
    run = Runtime(
        MemoryStore(),
        meters=[StepMeter(), TokenmasterMeter(meter=_tokenmaster_meter())],
    ).run("tokenmaster", budget=Budget(tokens=100, steps=10))

    node = run.model_call(
        {"model": "test:model"},
        fn=lambda _payload: {"usage": {"input_tokens": 30, "output_tokens": 5}},
    )

    assert node.meta["charges"]["tokens"] == 35
    assert node.meta["tokenmaster"]["state"]["used_tokens"] == 35


class FixedEstimator:
    def estimate_input_tokens(self, payload: dict[str, object]) -> int:
        assert payload["model"] == "test:model"
        return 7


def test_tokenmaster_meter_estimates_input_and_reserves_output() -> None:
    meter = TokenmasterMeter(
        meter=_tokenmaster_meter(),
        estimator=FixedEstimator(),
        reserved_output=5,
    )

    assert meter.precheck_estimate("model_call", {"model": "test:model"}) == 12
    assert meter.precheck_estimate("tool_call", {}) is None
    assert meter.precheck_is_estimate is True


@pytest.mark.parametrize("estimate", [-1, True, 1.5])
def test_tokenmaster_meter_rejects_invalid_estimates(estimate: object) -> None:
    class InvalidEstimator:
        def estimate_input_tokens(self, payload: dict[str, object]) -> object:
            del payload
            return estimate

    meter = TokenmasterMeter(estimator=InvalidEstimator())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative int or None"):
        meter.precheck_estimate("model_call", {})


@pytest.mark.parametrize("reserved_output", [-1, True, 1.5])
def test_tokenmaster_meter_rejects_invalid_output_reservation(
    reserved_output: object,
) -> None:
    with pytest.raises(ValueError, match="reserved_output"):
        TokenmasterMeter(reserved_output=reserved_output)  # type: ignore[arg-type]


def test_tokenmaster_estimator_refuses_before_model_dispatch() -> None:
    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    run = Runtime(
        MemoryStore(),
        meters=[
            StepMeter(),
            TokenmasterMeter(
                meter=_tokenmaster_meter(),
                estimator=FixedEstimator(),
                reserved_output=5,
            ),
        ],
    ).run("tokenmaster-estimated-refusal", budget=Budget(tokens=11))

    with pytest.raises(BudgetExceeded) as exc_info:
        run.model_call({"model": "test:model"}, fn=fn)

    refusal = run.store.get(exc_info.value.refusal_id)
    assert refusal.payload["estimated"] == "true"
    assert refusal.payload["requested"] == "12"
    assert not called


def test_tokenmaster_estimate_is_conservative_fallback_without_usage() -> None:
    run = Runtime(
        MemoryStore(),
        meters=[
            StepMeter(),
            TokenmasterMeter(
                meter=_tokenmaster_meter(),
                estimator=FixedEstimator(),
                reserved_output=5,
            ),
        ],
    ).run("tokenmaster-estimated-fallback", budget=Budget(tokens=20))

    with pytest.warns(UserWarning, match="no compatible usage"):
        node = run.model_call(
            {"model": "test:model"},
            fn=lambda _payload: {"text": "completed without usage"},
        )

    assert node.meta["charges"]["tokens"] == 12
    assert node.meta["accounting_fallbacks"]["tokens"] == {
        "reason": "missing_or_invalid_provider_usage",
        "source": "precheck_estimate",
    }
