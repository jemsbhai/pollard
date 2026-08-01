import asyncio
import warnings
from decimal import Decimal

import pytest
from tokenmaster import CalibrationRecord, Meter, ModelProfile, Pricing

from pollard import AsyncRuntime, Budget, BudgetExceeded, MemoryStore, Runtime
from pollard.meters import (
    DepthMeter,
    MeterPrecheckRefusal,
    StepMeter,
    TokenmasterCostMeter,
    TokenmasterMeter,
    WallClockMeter,
    tokenmaster_governance_meters,
)


def _tokenmaster_meter() -> Meter:
    return Meter(
        ModelProfile(
            model_id="test:model",
            provider="test",
            window_nominal=1_000,
        )
    )


def test_tokenmaster_governance_factory_preserves_standard_runtime_accounting() -> None:
    meters = tokenmaster_governance_meters(
        "gpt-5.6",
        estimator=ConstantEstimator(10),
        reserved_output=2,
        expected_remaining_turns=3,
        enforce_profile_limits=True,
    )

    assert [type(meter) for meter in meters] == [
        StepMeter,
        DepthMeter,
        WallClockMeter,
        TokenmasterMeter,
        TokenmasterCostMeter,
    ]
    assert [meter.name for meter in meters] == [
        "steps",
        "depth",
        "seconds",
        "tokens",
        "usd",
    ]


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


class ConstantEstimator:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def estimate_input_tokens(self, payload: dict[str, object]) -> int | None:
        del payload
        return self.value


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


def test_profile_enforcement_requires_an_estimator_and_known_capacity_kind() -> None:
    with pytest.raises(ValueError, match="requires an estimator"):
        TokenmasterMeter(enforce_profile_limits=True)
    with pytest.raises(ValueError, match="profile_capacity"):
        TokenmasterMeter(profile_capacity="marketing")


def test_legacy_estimator_does_not_parse_provider_output_options() -> None:
    meter = TokenmasterMeter(estimator=ConstantEstimator(7), reserved_output=5)
    assert meter.precheck_estimate("model_call", {"max_tokens": "provider-valid"}) == 12


def test_gpt_5_6_alias_accepts_exact_input_and_context_boundaries() -> None:
    meter = TokenmasterMeter(
        model="openai:gpt-5.6",
        estimator=ConstantEstimator(922_000),
        reserved_output=128_000,
        enforce_profile_limits=True,
    )

    assert meter.precheck_estimate("model_call", {}) == 1_050_000


def test_gpt_5_6_profile_refusal_is_auditable_before_dispatch() -> None:
    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}

    store = MemoryStore()
    run = Runtime(
        store,
        meters=[
            StepMeter(),
            TokenmasterMeter(
                model="gpt-5.6",
                estimator=ConstantEstimator(922_001),
                enforce_profile_limits=True,
            ),
        ],
    ).run("gpt-5.6-profile-limit")

    with pytest.raises(BudgetExceeded) as exc_info:
        run.model_call({"model": "ignored-by-binding"}, fn=fn)

    refusal = store.get(exc_info.value.refusal_id)
    assert refusal.payload["reason"] == "tokenmaster_profile_limit"
    assert refusal.payload["meter"] == "tokens"
    assert refusal.payload["requested"] == "922001"
    assert refusal.payload["remaining"] == "922000"
    assert refusal.meta["tokenmaster"]["model_id"] == "openai:gpt-5.6-sol"
    assert refusal.meta["tokenmaster"]["limits"]["input_exceeded"] is True
    assert not called


@pytest.mark.parametrize(
    "output_key",
    ["max_output_tokens", "max_completion_tokens", "max_tokens"],
)
def test_profile_enforcement_understands_requested_output_spellings(
    output_key: str,
) -> None:
    meter = TokenmasterMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(0),
        enforce_profile_limits=True,
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {output_key: 128_001})

    assert exc_info.value.reason == "tokenmaster_profile_limit"
    assert exc_info.value.requested == "128001"
    assert exc_info.value.remaining == "128000"
    limits = exc_info.value.audit_meta["tokenmaster"]["limits"]
    assert limits["output_exceeded"] is True


def test_profile_enforcement_can_use_effective_capacity() -> None:
    profile = ModelProfile(
        model_id="test:effective",
        provider="test",
        window_nominal=1_000,
        max_output=100,
        effective=CalibrationRecord(
            model_id="test:effective",
            effective_context=800,
            method="test",
            source="test-suite",
        ),
    )
    meter = TokenmasterMeter(
        meter=Meter(profile),
        estimator=ConstantEstimator(701),
        enforce_profile_limits=True,
        profile_capacity="effective",
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    limits = exc_info.value.audit_meta["tokenmaster"]["limits"]
    assert limits["capacity"] == 800
    assert limits["max_input_tokens"] == 700


def test_profile_enforcement_refuses_when_estimator_has_no_answer() -> None:
    meter = TokenmasterMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(None),
        enforce_profile_limits=True,
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    assert exc_info.value.reason == "tokenmaster_profile_unavailable"
    assert (
        exc_info.value.audit_meta["tokenmaster"]["limits"]["reason"]
        == "missing_input_estimate"
    )


def test_settlement_records_profile_overage_without_raising() -> None:
    meter = TokenmasterMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(1),
        enforce_profile_limits=True,
    )
    meta: dict[str, object] = {}

    charge = meter.charge(
        "model_call",
        {"model": "gpt-5.6"},
        {
            "model": "openai:gpt-5.6-sol",
            "usage": {"input_tokens": 922_001, "output_tokens": 1},
        },
        meta,
    )

    assert charge == 922_002
    assert meta["tokenmaster"]["limits"]["allowed"] is False
    assert meta["tokenmaster"]["limits"]["phase"] == "settlement"


def test_result_model_is_inferred_but_explicit_binding_wins() -> None:
    inferred = TokenmasterMeter()
    inferred_meta: dict[str, object] = {}
    inferred.charge(
        "model_call",
        {"model": "openai:gpt-5.5"},
        {
            "model": "gpt-5.6",
            "usage": {"model_id": "openai:gpt-5.4", "input_tokens": 1},
        },
        inferred_meta,
    )
    assert inferred_meta["tokenmaster"]["turn"]["model_id"] == "openai:gpt-5.6-sol"

    bound = TokenmasterMeter(model="openai:gpt-5.5")
    bound_meta: dict[str, object] = {}
    bound.charge(
        "model_call",
        {"model": "gpt-5.4"},
        {"model": "gpt-5.6", "usage": {"input_tokens": 1}},
        bound_meta,
    )
    assert bound_meta["tokenmaster"]["turn"]["model_id"] == "openai:gpt-5.5"


def test_inferred_model_binding_is_not_sticky_across_calls() -> None:
    meter = TokenmasterMeter()
    first_meta: dict[str, object] = {}
    second_meta: dict[str, object] = {}

    meter.charge(
        "model_call",
        {"model": "gpt-5.5"},
        {"model": "gpt-5.5", "usage": {"input_tokens": 1}},
        first_meta,
    )
    meter.charge(
        "model_call",
        {"model": "gpt-5.5"},
        {"model": "gpt-5.6", "usage": {"input_tokens": 2}},
        second_meta,
    )

    assert first_meta["tokenmaster"]["turn"]["model_id"] == "openai:gpt-5.5"
    assert second_meta["tokenmaster"]["turn"]["model_id"] == "openai:gpt-5.6-sol"
    assert second_meta["tokenmaster"]["state"]["used_tokens"] == 2


def test_openai_nested_details_are_made_exclusive_and_metadata_merges() -> None:
    profile = ModelProfile(
        model_id="test:priced",
        provider="test",
        window_nominal=10_000,
        pricing=Pricing(input=10, cache_read=1, cache_write=12, output=20),
    )
    underlying = Meter(profile)
    token_meter = TokenmasterMeter(meter=underlying)
    cost_meter = TokenmasterCostMeter(
        meter=underlying,
        estimator=ConstantEstimator(100),
    )
    result = {
        "usage": {"input_tokens": 100, "output_tokens": 40},
        "provider_usage": {
            "input_tokens": 100,
            "input_tokens_details": {
                "cached_tokens": 20,
                "cache_write_tokens": 30,
            },
            "output_tokens": 40,
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }
    meta: dict[str, object] = {}

    cost = cost_meter.charge("model_call", {"model": "provider:model"}, result, meta)
    tokens = token_meter.charge("model_call", {"model": "provider:model"}, result, meta)

    assert tokens == 140
    turn = meta["tokenmaster"]["turn"]
    assert turn["input_tokens"] == 50
    assert turn["cache_read_tokens"] == 20
    assert turn["cache_write_tokens"] == 30
    assert turn["output_tokens"] == 30
    assert turn["reasoning_tokens"] == 10
    assert cost == Decimal("0.00168")
    assert meta["tokenmaster"]["cost"]["status"] == "quoted"


@pytest.mark.parametrize(
    ("provider_usage", "expected"),
    [
        (
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
                "output_tokens": 4,
            },
            (10, 2, 3, 4),
        ),
        (
            {
                "inputTokens": 10,
                "cacheReadInputTokens": 2,
                "cacheWriteInputTokens": 3,
                "outputTokens": 4,
            },
            (10, 2, 3, 4),
        ),
    ],
)
def test_anthropic_and_bedrock_cache_fields_are_already_exclusive(
    provider_usage: dict[str, int],
    expected: tuple[int, int, int, int],
) -> None:
    meter = TokenmasterMeter(meter=_tokenmaster_meter())
    meta: dict[str, object] = {}

    charge = meter.charge(
        "model_call",
        {"model": "test:model"},
        {
            "usage": {"input_tokens": 15, "output_tokens": 4},
            "provider_usage": provider_usage,
        },
        meta,
    )

    turn = meta["tokenmaster"]["turn"]
    assert charge == 19
    assert (
        turn["input_tokens"],
        turn["cache_read_tokens"],
        turn["cache_write_tokens"],
        turn["output_tokens"],
    ) == expected


def test_gpt_5_6_cost_meter_uses_conservative_long_tier_precheck() -> None:
    meter = TokenmasterCostMeter(
        model="openai:gpt-5.6",
        estimator=ConstantEstimator(300_000),
        reserved_output=10_000,
    )

    assert meter.precheck_estimate("model_call", {}) == Decimal("4.2")


def test_gpt_5_6_cost_meter_exact_long_tier_is_decimal_safe() -> None:
    meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(300_000),
    )
    meta: dict[str, object] = {}

    charge = meter.charge(
        "model_call",
        {"model": "ignored"},
        {
            "model": "google:gemini-3.1-pro",
            "usage": {"input_tokens": 300_000, "output_tokens": 10_000},
            "provider_usage": {
                "input_tokens": 300_000,
                "input_tokens_details": {
                    "cached_tokens": 100_000,
                    "cache_write_tokens": 50_000,
                },
                "output_tokens": 10_000,
                "output_tokens_details": {"reasoning_tokens": 2_000},
            },
        },
        meta,
    )

    assert charge == Decimal("2.675")
    assert meta["tokenmaster"]["cost"]["model_id"] == "openai:gpt-5.6-sol"
    assert meta["tokenmaster"]["cost"]["tier_min_input_tokens"] == 272_001
    assert meta["tokenmaster"]["cost"]["total_cost_decimal"] == "2.675"


def test_cost_meter_refuses_missing_pricing_before_dispatch() -> None:
    profile = ModelProfile(
        model_id="test:unpriced",
        provider="test",
        window_nominal=1_000,
    )
    meter = TokenmasterCostMeter(
        meter=Meter(profile),
        estimator=ConstantEstimator(1),
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    assert exc_info.value.reason == "tokenmaster_pricing_unavailable"
    assert (
        exc_info.value.audit_meta["tokenmaster"]["pricing"]["reason"]
        == "pricing_lookup_failed"
    )


def test_cost_meter_refuses_non_usd_pricing_before_dispatch() -> None:
    profile = ModelProfile(
        model_id="test:eur",
        provider="test",
        window_nominal=1_000,
        pricing=Pricing(input=1, output=2, currency="EUR"),
    )
    meter = TokenmasterCostMeter(
        meter=Meter(profile),
        estimator=ConstantEstimator(1),
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    assert exc_info.value.reason == "tokenmaster_currency"
    assert exc_info.value.audit_meta["tokenmaster"]["pricing"] == {
        "status": "unsupported_currency",
        "currency": "EUR",
    }


def test_gemini_cache_storage_is_incomplete_and_fails_closed() -> None:
    meter = TokenmasterCostMeter(
        model="google:gemini-3.1-pro",
        estimator=ConstantEstimator(1),
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    pricing = exc_info.value.audit_meta["tokenmaster"]["pricing"]
    assert pricing["reason"] == "pricing_lookup_failed"
    assert "cache_write_tokens" in pricing["error"]


def test_cost_meter_refuses_when_estimator_has_no_answer() -> None:
    meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(None),
    )

    with pytest.raises(MeterPrecheckRefusal) as exc_info:
        meter.precheck_estimate("model_call", {})

    assert (
        exc_info.value.audit_meta["tokenmaster"]["pricing"]["reason"]
        == "missing_input_estimate"
    )


def test_cost_budget_refuses_before_provider_dispatch() -> None:
    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}

    store = MemoryStore()
    run = Runtime(
        store,
        meters=[
            StepMeter(),
            TokenmasterCostMeter(
                model="gpt-5.6",
                estimator=ConstantEstimator(300_000),
                reserved_output=10_000,
            ),
        ],
    ).run("tokenmaster-usd-refusal", budget=Budget(usd="4.19", steps=2))

    with pytest.raises(BudgetExceeded) as exc_info:
        run.model_call({"model": "gpt-5.6"}, fn=fn)

    refusal = store.get(exc_info.value.refusal_id)
    assert refusal.payload["reason"] == "budget"
    assert refusal.payload["meter"] == "usd"
    assert refusal.payload["requested"] == "4.2"
    assert not called


def test_cost_settlement_failure_uses_reserved_precheck_estimate() -> None:
    payload = {"model": "gpt-5.6", "input": "hello"}
    meter = TokenmasterCostMeter(
        estimator=ConstantEstimator(10),
        reserved_output=2,
    )
    expected = meter.precheck_estimate("model_call", payload)
    run = Runtime(MemoryStore(), meters=[StepMeter(), meter]).run(
        "tokenmaster-usd-settlement-fallback",
        budget=Budget(usd="1", steps=2),
    )

    node = run.model_call(
        payload,
        fn=lambda _payload: {
            "model": "gateway:unregistered-result",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    )

    assert Decimal(str(node.meta["charges"]["usd"])) == expected
    assert node.meta["tokenmaster"]["cost"]["status"] == "unavailable"
    assert node.meta["accounting_fallbacks"]["usd"] == {
        "reason": "exact_pricing_unavailable",
        "source": "precheck_estimate",
    }


def test_post_dispatch_dependency_and_pricing_failures_do_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> object:
        raise RuntimeError("dependency unavailable after dispatch")

    monkeypatch.setattr("pollard.meters.tokenmaster._load_tokenmaster", unavailable)
    result = {"usage": {"input_tokens": 3, "output_tokens": 2}}

    token_meta: dict[str, object] = {}
    token_meter = TokenmasterMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(1),
        enforce_profile_limits=True,
    )
    with pytest.warns(UserWarning, match="could not record"):
        assert token_meter.charge("model_call", {}, result, token_meta) == 5
    assert token_meta["tokenmaster"]["limits"]["status"] == "unavailable"
    assert token_meta["tokenmaster"]["meter"]["status"] == "error"

    cost_meta: dict[str, object] = {}
    cost_meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(1),
    )
    assert cost_meter.charge("model_call", {}, result, cost_meta) == Decimal("0")
    assert cost_meta["tokenmaster"]["cost"]["status"] == "unavailable"


def test_post_dispatch_diagnostics_do_not_raise_when_warnings_are_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_meter = TokenmasterMeter()
    cost_meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(1),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert token_meter.charge("model_call", {}, {"text": "done"}, {}) == 0
        assert cost_meter.charge("model_call", {}, {"text": "done"}, {}) == Decimal("0")

        def unavailable() -> object:
            raise RuntimeError("dependency unavailable after dispatch")

        monkeypatch.setattr("pollard.meters.tokenmaster._load_tokenmaster", unavailable)
        token_meta: dict[str, object] = {}
        cost_meta: dict[str, object] = {}
        assert TokenmasterMeter(model="gpt-5.6").charge(
            "model_call",
            {},
            {"usage": {"input_tokens": 3, "output_tokens": 2}},
            token_meta,
        ) == 5
        assert cost_meter.charge(
            "model_call",
            {},
            {"usage": {"input_tokens": 3, "output_tokens": 2}},
            cost_meta,
        ) == Decimal("0")
        assert token_meta["tokenmaster"]["meter"]["status"] == "error"
        assert cost_meta["tokenmaster"]["cost"]["status"] == "unavailable"


def test_tokenmaster_cost_meter_runs_in_sync_and_strict_replay() -> None:
    store = MemoryStore()
    live_meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(10),
        reserved_output=2,
    )
    live = Runtime(store, mode="record", meters=[StepMeter(), live_meter]).run(
        "tokenmaster-cost-replay",
        budget=Budget(usd="1", steps=2),
    )
    recorded = live.model_call(
        {"model": "gpt-5.6"},
        fn=lambda _payload: {"usage": {"input_tokens": 10, "output_tokens": 2}},
    )

    replay_meter = TokenmasterCostMeter(
        model="gpt-5.6",
        estimator=ConstantEstimator(10),
        reserved_output=2,
    )
    replay = Runtime(store, mode="replay", meters=[StepMeter(), replay_meter]).run(
        "tokenmaster-cost-replay"
    )
    replayed = replay.model_call(
        {"model": "gpt-5.6"},
        fn=lambda _payload: pytest.fail("strict replay must not dispatch"),
    )

    assert replayed.id == recorded.id
    assert replay.report()["avoided"]["usd"] == recorded.meta["charges"]["usd"]


def test_tokenmaster_cost_meter_runs_in_async_runtime() -> None:
    async def scenario() -> None:
        meter = TokenmasterCostMeter(
            model="gpt-5.6",
            estimator=ConstantEstimator(10),
            reserved_output=2,
        )
        run = AsyncRuntime(
            MemoryStore(),
            meters=[StepMeter(), meter],
        ).run("tokenmaster-cost-async", budget=Budget(usd="1", steps=2))

        async def result(_payload: dict[str, object]) -> dict[str, object]:
            return {"usage": {"input_tokens": 10, "output_tokens": 2}}

        node = await run.amodel_call({"model": "gpt-5.6"}, fn=result)
        assert node.meta["charges"]["usd"] > 0
        assert node.meta["tokenmaster"]["cost"]["status"] == "quoted"

    asyncio.run(scenario())
