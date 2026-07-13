import pytest
from tokenmaster import Meter, ModelProfile

from pollard import Budget, MemoryStore, Runtime
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
