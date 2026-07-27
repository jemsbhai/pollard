import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from pollard import (
    AsyncRuntime,
    Budget,
    BudgetExceeded,
    ExactResultComparator,
    IntegrityError,
    MemoryStore,
    MissingRecording,
    Node,
    NodeKind,
    NormalizedModelComparator,
    ReplayContract,
    RevalidationComparison,
    Runtime,
    Store,
    verify,
)
from pollard.revalidation import MAX_DIFFERENCE_PATHS, make_revalidation_payload

PAYLOAD = {
    "model": "mock-1",
    "messages": [{"role": "user", "content": "revalidate this"}],
    "_pollard": {"provider": "mock"},
}
RECORDED_RESULT = {
    "id": "recorded-id",
    "text": "stable answer",
    "usage": {"input_tokens": 3, "output_tokens": 2},
    "provider_usage": {"input_tokens": 3, "output_tokens": 2},
}


class CapturingMeter:
    name = "captured"

    def __init__(self) -> None:
        self.prechecks: list[dict[str, Any]] = []
        self.charges: list[dict[str, Any]] = []

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, Any],
    ) -> None:
        assert node_kind == "model_call"
        self.prechecks.append(deepcopy(payload))
        return None

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> int:
        del result, meta
        assert node_kind == "model_call"
        self.charges.append(deepcopy(payload))
        return 0


class FailingComparisonEvidenceStore(MemoryStore):
    def put(self, node: Node) -> None:
        if node.payload.get("event") == "model_revalidation_comparison_failed":
            raise OSError("comparison evidence unavailable")
        super().put(node)


def _record(
    store: MemoryStore,
    *,
    label: str = "revalidation",
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    recorded_contract = ReplayContract(
        provider="mock",
        model_revision="snapshot-1",
        adapter="tests.mock",
        adapter_version="1",
        application_revision="abc123",
    )
    bound = recorded_contract.bind(PAYLOAD if payload is None else payload)
    node = Runtime(store).run(label).model_call(
        bound,
        fn=lambda _payload: RECORDED_RESULT if result is None else result,
    )
    return bound, node


def test_replay_contract_binds_copy_and_preserves_reserved_metadata() -> None:
    environment = {"region": "us-east", "flags": ["deterministic"]}
    contract = ReplayContract(
        provider="mock",
        model_revision="snapshot-1",
        api_version="2026-07-01",
        adapter="tests.mock",
        adapter_version="2",
        sdk="mock-sdk",
        sdk_version="3",
        application_revision="abc123",
        environment=environment,
    )
    source = deepcopy(PAYLOAD)

    bound = contract.bind(source)
    rebound = contract.bind(bound)
    environment["region"] = "mutated"

    assert source == PAYLOAD
    assert bound == rebound
    assert bound["_pollard"]["provider"] == "mock"
    fingerprint = bound["_pollard"]["replay_contract"]
    assert fingerprint == {
        "format": "pollard/replay-contract/v1",
        "provider": "mock",
        "model_revision": "snapshot-1",
        "api_version": "2026-07-01",
        "adapter": "tests.mock",
        "adapter_version": "2",
        "sdk": "mock-sdk",
        "sdk_version": "3",
        "application_revision": "abc123",
        "environment": {"region": "us-east", "flags": ["deterministic"]},
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"provider": ""}, "provider"),
        ({"provider": "mock", "model_revision": " "}, "model_revision"),
        ({"provider": "mock", "environment": {"temperature": 0.0}}, "floats"),
        ({"provider": "mock", "environment": ["invalid"]}, "environment"),
    ],
)
def test_replay_contract_rejects_invalid_identity(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        ReplayContract(**kwargs)


def test_replay_contract_rejects_nonobject_or_conflicting_reserved_metadata() -> None:
    contract = ReplayContract(provider="mock")
    with pytest.raises(TypeError, match="_pollard"):
        contract.bind({"model": "mock", "_pollard": "invalid"})

    other = ReplayContract(provider="other").bind({"model": "mock"})
    with pytest.raises(ValueError, match="different replay contract"):
        contract.bind(other)


def test_comparators_report_value_free_json_pointer_differences() -> None:
    exact = ExactResultComparator().compare(
        {"a/b": {"~key": True}, "count": 1},
        {"a/b": {"~key": 1}, "count": 2},
    )
    assert not exact.matched
    assert exact.difference_paths == ("/a~1b/~0key", "/count")
    assert not exact.truncated

    with pytest.raises(ValueError, match="JSON pointers"):
        RevalidationComparison(matched=False, difference_paths=("not-a-pointer",))
    with pytest.raises(TypeError, match="matched"):
        RevalidationComparison(matched=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="truncated"):
        RevalidationComparison(matched=True, truncated=1)  # type: ignore[arg-type]
    coerced = RevalidationComparison(
        matched=False,
        difference_paths=["/field"],  # type: ignore[arg-type]
    )
    assert coerced.difference_paths == ("/field",)
    with pytest.raises(ValueError, match="matched comparison"):
        RevalidationComparison(matched=True, difference_paths=("/field",))
    with pytest.raises(ValueError, match="cannot exceed"):
        RevalidationComparison(
            matched=False,
            difference_paths=tuple(
                f"/{index}" for index in range(MAX_DIFFERENCE_PATHS + 1)
            ),
        )


def test_exact_comparator_bounds_difference_paths() -> None:
    recorded = {f"key-{index:03d}": index for index in range(MAX_DIFFERENCE_PATHS + 5)}
    live = {key: value + 1 for key, value in recorded.items()}

    comparison = ExactResultComparator().compare(recorded, live)

    assert not comparison.matched
    assert len(comparison.difference_paths) == MAX_DIFFERENCE_PATHS
    assert comparison.truncated


def test_normalized_comparator_ignores_transport_and_tool_call_ids() -> None:
    recorded = {
        "id": "response-a",
        "text": "same",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "chunks": [{"delta": {"text": "same"}}],
        "tool_calls": [
            {
                "id": "call-a",
                "index": 0,
                "function": {"name": "lookup", "arguments": '{"b":2,"a":1}'},
            }
        ],
    }
    live = {
        "id": "response-b",
        "text": "same",
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "tool_calls": [
            {
                "id": "call-b",
                "index": 9,
                "function": {"name": "lookup", "arguments": '{ "a": 1, "b": 2 }'},
            }
        ],
    }

    assert NormalizedModelComparator().compare(recorded, live).matched
    changed = deepcopy(live)
    changed["tool_calls"][0]["function"]["arguments"] = '{"a":1,"b":3}'
    comparison = NormalizedModelComparator().compare(recorded, changed)
    assert not comparison.matched
    assert comparison.difference_paths == ("/tool_calls/0/function/arguments/b",)


def test_normalized_comparator_falls_back_to_nonaccounting_result_fields() -> None:
    comparator = NormalizedModelComparator()
    assert comparator.compare(
        {"answer": {"value": 1}, "usage": {"input_tokens": 1}},
        {"answer": {"value": 1}, "usage": {"input_tokens": 99}},
    ).matched
    comparison = comparator.compare(
        {"answer": {"value": 1}},
        {"answer": {"value": 2}},
    )
    assert comparison.difference_paths == ("/answer/value",)


def test_normalized_comparator_handles_provider_tool_variants_and_invalid_shapes() -> None:
    comparator = NormalizedModelComparator()
    assert comparator.compare(
        {"tool_calls": {"unexpected": True}},
        {"tool_calls": {"unexpected": True}},
    ).matched
    assert comparator.compare(
        {"tool_calls": ["opaque"]},
        {"tool_calls": ["opaque"]},
    ).matched
    assert comparator.compare(
        {
            "tool_calls": [
                {
                    "name": "lookup",
                    "input_json": '{"key":"value"}',
                    "type": "function",
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "lookup",
                    "input_json": '{ "key": "value" }',
                    "type": "function",
                }
            ]
        },
    ).matched
    assert comparator.compare(
        {
            "tool_calls": [
                {"function": {"name": "lookup", "arguments": {"key": 1}}}
            ]
        },
        {
            "tool_calls": [
                {"function": {"name": "lookup", "arguments": {"key": 1}}}
            ]
        },
    ).matched
    invalid_json = comparator.compare(
        {"tool_calls": [{"name": "lookup", "input_json": "not-json"}]},
        {"tool_calls": [{"name": "lookup", "input_json": "different"}]},
    )
    assert invalid_json.difference_paths == ("/tool_calls/0/input_json",)
    assert comparator.compare(
        {
            "tool_calls": [
                {"call_id": "call-a", "name": "lookup", "arguments": '{"key":1}'}
            ]
        },
        {
            "tool_calls": [
                {
                    "call_id": "call-b",
                    "name": "lookup",
                    "arguments": '{ "key": 1 }',
                }
            ]
        },
    ).matched


def test_exact_comparator_reports_extra_list_items() -> None:
    comparison = ExactResultComparator().compare(
        {"items": [1]},
        {"items": [1, 2]},
    )
    assert comparison.difference_paths == ("/items/1",)


def test_revalidation_payload_validates_reserved_metadata_shapes() -> None:
    contract = ReplayContract(provider="mock")
    marked = make_revalidation_payload(
        {"model": "mock"},
        observation_id="no-metadata",
        recorded_node_id="a" * 64,
        recorded_result_digest="b" * 64,
        contract=contract,
        comparator_name="custom/v1",
    )
    assert marked["_pollard"]["revalidation"]["observation_id"] == "no-metadata"

    with pytest.raises(TypeError, match="_pollard"):
        make_revalidation_payload(
            {"model": "mock", "_pollard": "invalid"},
            observation_id="bad-metadata",
            recorded_node_id="a" * 64,
            recorded_result_digest="b" * 64,
            contract=contract,
            comparator_name="custom/v1",
        )


def test_live_revalidation_records_separate_budgeted_evidence_and_preserves_golden() -> None:
    store = MemoryStore()
    payload, golden = _record(store)
    golden_before = store.get(golden.id)
    seen: list[dict[str, Any]] = []
    live_contract = ReplayContract(
        provider="mock",
        model_revision="snapshot-2",
        adapter="tests.mock",
        adapter_version="2",
        environment={"region": "us-east"},
    )
    live_result = {
        "id": "live-id",
        "text": "stable answer",
        "usage": {"input_tokens": 4, "output_tokens": 3},
        "provider_usage": {"input_tokens": 4, "output_tokens": 3},
    }
    run = Runtime(store, mode="record").run("revalidation")

    report = run.revalidate_model_call(
        payload,
        fn=lambda received: seen.append(deepcopy(received)) or live_result,
        contract=live_contract,
        observation_id="observation-1",
    )

    assert seen == [payload]
    assert report.matched
    assert not report.exact_match
    assert report.difference_paths == ()
    assert report.charges["steps"] == 1
    assert report.charges["tokens"] == 7
    assert report.charges["seconds"] >= 0
    assert report.recorded_node_id == golden.id
    assert report.recorded_contract["model_revision"] == "snapshot-1"
    assert report.live_contract["model_revision"] == "snapshot-2"
    assert run.cursor_id == golden.id
    assert store.get(golden.id) == golden_before

    live = store.get(report.live_node_id)
    evidence = store.get(report.evidence_node_id)
    assert live.parent == run.root_id
    assert live.id != golden.id
    assert live.result == live_result
    marker = live.payload["_pollard"]["revalidation"]
    assert marker["recorded_node_id"] == golden.id
    assert marker["recorded_result_digest"] == golden.result_digest
    assert marker["live_contract"]["model_revision"] == "snapshot-2"
    assert evidence.parent == live.id
    assert evidence.payload["event"] == "model_revalidation"
    assert evidence.payload["comparison"]["matched"] is True
    assert "stable answer" not in json.dumps(evidence.payload)

    document = report.to_dict()
    document["live_contract"]["provider"] = "mutated"
    assert report.live_contract["provider"] == "mock"


def test_revalidation_store_parity(store: Store) -> None:
    contract = ReplayContract(provider="mock", model_revision="store-v1")
    payload = contract.bind({"model": "mock", "messages": []})
    golden = Runtime(store).run("revalidation-store-parity").model_call(
        payload,
        fn=lambda _payload: RECORDED_RESULT,
    )

    report = Runtime(store).run(
        "revalidation-store-parity"
    ).revalidate_model_call(
        payload,
        fn=lambda _payload: {**RECORDED_RESULT, "id": "live-store-id"},
        contract=ReplayContract(provider="mock", model_revision="store-v2"),
    )

    assert report.matched
    assert store.get(golden.id).result == RECORDED_RESULT
    assert verify(store, report.evidence_node_id).ok


def test_revalidation_supports_legacy_recording_without_bound_contract() -> None:
    store = MemoryStore()
    payload = {"model": "legacy", "messages": []}
    golden = Runtime(store).run("legacy-revalidation").model_call(
        payload,
        fn=lambda _payload: RECORDED_RESULT,
    )

    report = Runtime(store).run("legacy-revalidation").revalidate_model_call(
        payload,
        fn=lambda _payload: RECORDED_RESULT,
        contract=ReplayContract(provider="mock", model_revision="current"),
    )

    assert report.matched
    assert report.recorded_node_id == golden.id
    assert report.recorded_contract is None
    evidence = store.get(report.evidence_node_id)
    assert "recorded_contract" not in evidence.payload


def test_revalidation_compares_canonical_stored_json_across_python_container_types() -> None:
    store = MemoryStore()
    payload = {"model": "canonical-json"}
    Runtime(store).run("canonical-revalidation").model_call(
        payload,
        fn=lambda _payload: {
            "values": (1, 2),
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )

    report = Runtime(store).run(
        "canonical-revalidation"
    ).revalidate_model_call(
        payload,
        fn=lambda _payload: {
            "values": [1, 2],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
        contract=ReplayContract(provider="local"),
        comparator=ExactResultComparator(),
    )

    assert report.matched
    assert report.exact_match


def test_exact_revalidation_reports_drift_paths_without_values() -> None:
    store = MemoryStore()
    payload, golden = _record(store)
    run = Runtime(store).run("revalidation")

    report = run.revalidate_model_call(
        payload,
        fn=lambda _payload: {
            "id": "live-secret-id",
            "text": "different secret output",
            "usage": {"input_tokens": 3, "output_tokens": 3},
        },
        contract=ReplayContract(provider="mock", model_revision="snapshot-2"),
        comparator=ExactResultComparator(),
        observation_id="exact-drift",
    )

    assert not report.matched
    assert not report.exact_match
    assert report.difference_paths == (
        "/id",
        "/provider_usage",
        "/text",
        "/usage/output_tokens",
    )
    evidence = store.get(report.evidence_node_id)
    encoded = json.dumps(evidence.payload)
    assert "different secret output" not in encoded
    assert "live-secret-id" not in encoded
    assert run.cursor_id == golden.id


def test_revalidation_can_execute_a_distinct_live_request_and_fingerprint() -> None:
    store = MemoryStore()
    payload, golden = _record(store)
    live_contract = ReplayContract(provider="mock", model_revision="snapshot-2")
    live_payload = live_contract.bind(
        {
            "model": "mock-2",
            "messages": [{"role": "user", "content": "revalidate this"}],
            "_pollard": {"provider": "mock"},
        }
    )
    seen: list[dict[str, Any]] = []
    run = Runtime(store).run("revalidation")

    report = run.revalidate_model_call(
        payload,
        live_payload=live_payload,
        fn=lambda received: seen.append(deepcopy(received)) or RECORDED_RESULT,
        contract=live_contract,
        observation_id="new-request",
    )

    assert seen == [live_payload]
    assert report.matched
    assert run.cursor_id == golden.id
    observation = store.get(report.live_node_id)
    assert observation.payload["model"] == "mock-2"
    assert (
        observation.payload["_pollard"]["replay_contract"]
        == live_contract.to_dict()
    )


def test_revalidation_meters_receive_the_dispatched_payload_not_audit_metadata() -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    meter = CapturingMeter()
    live_payload = {"model": "mock-2", "messages": []}

    Runtime(store, meters=[meter]).run("revalidation").revalidate_model_call(
        payload,
        live_payload=live_payload,
        fn=lambda _payload: RECORDED_RESULT,
        contract=ReplayContract(provider="mock", model_revision="snapshot-2"),
    )

    assert meter.prechecks == [live_payload]
    assert meter.charges == [live_payload]
    assert "_pollard" not in meter.prechecks[0]


def test_revalidation_rejects_conflicting_live_payload_contract_before_dispatch() -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    declared = ReplayContract(provider="mock", model_revision="declared")
    conflicting_payload = ReplayContract(
        provider="mock",
        model_revision="different",
    ).bind({"model": "mock-2"})
    called = False

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return RECORDED_RESULT

    with pytest.raises(ValueError, match="does not match"):
        Runtime(store).run("revalidation").revalidate_model_call(
            payload,
            live_payload=conflicting_payload,
            fn=live,
            contract=declared,
        )
    assert not called


@pytest.mark.parametrize("mode", ["hybrid", "replay"])
def test_live_revalidation_rejects_nonrecord_modes_before_dispatch(mode: str) -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    called = False

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return RECORDED_RESULT

    run = Runtime(store, mode=mode).run("revalidation")
    with pytest.raises(RuntimeError, match="requires record mode"):
        run.revalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock"),
        )
    assert not called


def test_live_revalidation_rejects_dry_run_missing_recording_and_invalid_inputs() -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    called = False

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return RECORDED_RESULT

    with pytest.raises(RuntimeError, match="dry-run"):
        Runtime(store, dry_run=True).run("revalidation").revalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock"),
        )
    with pytest.raises(MissingRecording):
        Runtime(store).run("revalidation").revalidate_model_call(
            {**payload, "model": "missing"},
            fn=live,
            contract=ReplayContract(provider="mock"),
        )
    with pytest.raises(TypeError, match="ReplayContract"):
        Runtime(store).run("revalidation").revalidate_model_call(
            payload,
            fn=live,
            contract=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="comparator"):
        Runtime(store).run("revalidation").revalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock"),
            comparator=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="observation_id"):
        Runtime(store).run("revalidation").revalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock"),
            observation_id="",
        )
    assert not called


def test_duplicate_observation_id_is_refused_before_second_dispatch() -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    run = Runtime(store).run("revalidation")
    calls = 0

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return RECORDED_RESULT

    kwargs = {
        "fn": live,
        "contract": ReplayContract(provider="mock"),
        "observation_id": "unique-observation",
    }
    run.revalidate_model_call(payload, **kwargs)
    with pytest.raises(IntegrityError, match="already exists"):
        run.rollback(run.root_id)
        run.revalidate_model_call(payload, **kwargs)
    assert calls == 1


def test_reserved_revalidation_marker_and_malformed_recorded_contract_fail_closed() -> None:
    store = MemoryStore()
    marked = deepcopy(PAYLOAD)
    marked["_pollard"]["revalidation"] = {"untrusted": True}
    node = Runtime(store).run("reserved").model_call(
        marked,
        fn=lambda _payload: RECORDED_RESULT,
    )
    run = Runtime(store).run("reserved")
    with pytest.raises(ValueError, match="reserved"):
        run.revalidate_model_call(
            marked,
            fn=lambda _payload: pytest.fail("live callable executed"),
            contract=ReplayContract(provider="mock"),
        )
    assert run.cursor_id == run.root_id
    assert store.get(node.id).result == RECORDED_RESULT

    malformed = deepcopy(PAYLOAD)
    malformed["_pollard"]["replay_contract"] = "invalid"
    Runtime(store).run("malformed").model_call(
        malformed,
        fn=lambda _payload: RECORDED_RESULT,
    )
    with pytest.raises(IntegrityError, match="replay_contract"):
        Runtime(store).run("malformed").revalidate_model_call(
            malformed,
            fn=lambda _payload: pytest.fail("live callable executed"),
            contract=ReplayContract(provider="mock"),
        )


def test_revalidation_rejects_nonobject_recording_and_malformed_live_contract() -> None:
    store = MemoryStore()
    run = Runtime(store).run("nonobject-recording")
    payload = {"model": "legacy-nonobject"}
    store.put(
        Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=run.root_id,
            payload=payload,
            result=["not", "an", "object"],
        )
    )
    with pytest.raises(IntegrityError, match="not a replayable object"):
        Runtime(store).run("nonobject-recording").revalidate_model_call(
            payload,
            fn=lambda _payload: pytest.fail("live callable executed"),
            contract=ReplayContract(provider="mock"),
        )

    valid_payload, _golden = _record(store, label="malformed-live")
    malformed_live = {
        "model": "new",
        "_pollard": {"replay_contract": "not-an-object"},
    }
    with pytest.raises(ValueError, match="replay_contract"):
        Runtime(store).run("malformed-live").revalidate_model_call(
            valid_payload,
            live_payload=malformed_live,
            fn=lambda _payload: pytest.fail("live callable executed"),
            contract=ReplayContract(provider="mock"),
        )


def test_comparator_failure_records_content_free_failure_and_restores_cursor() -> None:
    class BrokenComparator:
        name = "broken/v1"

        def compare(
            self,
            recorded: dict[str, Any],
            live: dict[str, Any],
        ) -> RevalidationComparison:
            del recorded, live
            raise RuntimeError("sensitive comparator detail")

    store = MemoryStore()
    payload, golden = _record(store)
    run = Runtime(store).run("revalidation")

    with pytest.raises(RuntimeError, match="sensitive comparator detail"):
        run.revalidate_model_call(
            payload,
            fn=lambda _payload: RECORDED_RESULT,
            contract=ReplayContract(provider="mock"),
            comparator=BrokenComparator(),
            observation_id="broken-comparison",
        )

    assert run.cursor_id == golden.id
    failures = [
        node
        for node in store.walk(run.root_id)
        if node.payload.get("event") == "model_revalidation_comparison_failed"
    ]
    assert len(failures) == 1
    encoded = json.dumps(failures[0].payload)
    assert failures[0].payload["error_type"] == "RuntimeError"
    assert "sensitive comparator detail" not in encoded


def test_invalid_comparator_result_is_a_recorded_comparison_failure() -> None:
    class InvalidComparator:
        name = "invalid/v1"

        def compare(self, recorded: dict[str, Any], live: dict[str, Any]) -> object:
            del recorded, live
            return object()

    store = MemoryStore()
    payload, _golden = _record(store)
    run = Runtime(store).run("revalidation")
    with pytest.raises(TypeError, match="RevalidationComparison"):
        run.revalidate_model_call(
            payload,
            fn=lambda _payload: RECORDED_RESULT,
            contract=ReplayContract(provider="mock"),
            comparator=InvalidComparator(),  # type: ignore[arg-type]
        )


def test_comparator_failure_preserves_primary_error_when_failure_note_cannot_store() -> None:
    class BrokenComparator:
        name = "broken-cleanup/v1"

        def compare(
            self,
            recorded: dict[str, Any],
            live: dict[str, Any],
        ) -> RevalidationComparison:
            del recorded, live
            raise RuntimeError("primary comparator failure")

    store = FailingComparisonEvidenceStore()
    payload, golden = _record(store)  # type: ignore[arg-type]
    run = Runtime(store).run("revalidation")

    with pytest.raises(RuntimeError, match="primary comparator failure") as exc_info:
        run.revalidate_model_call(
            payload,
            fn=lambda _payload: RECORDED_RESULT,
            contract=ReplayContract(provider="mock"),
            comparator=BrokenComparator(),
        )

    assert isinstance(exc_info.value.__cause__, OSError)
    assert run.cursor_id == golden.id


def test_revalidation_budget_refusal_happens_before_live_dispatch() -> None:
    store = MemoryStore()
    payload, _golden = _record(store)
    called = False
    run = Runtime(store).run("revalidation", budget=Budget(steps=1))

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return RECORDED_RESULT

    with pytest.raises(BudgetExceeded):
        run.revalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock"),
        )
    assert not called


def test_revalidation_verifies_golden_integrity_before_live_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "revalidation.db"
    contract = ReplayContract(provider="mock")
    payload = contract.bind(PAYLOAD)
    Runtime(path).run("tampered-revalidation").model_call(
        payload,
        fn=lambda _payload: RECORDED_RESULT,
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE nodes SET result_digest = ? WHERE kind = ?",
            ("0" * 64, "model_call"),
        )
    called = False

    def live(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return RECORDED_RESULT

    with pytest.raises(IntegrityError, match="integrity"):
        Runtime(path).run("tampered-revalidation").revalidate_model_call(
            payload,
            fn=live,
            contract=contract,
        )
    assert not called


def test_sync_stream_revalidation_forwards_chunks_and_compares_final_result() -> None:
    store = MemoryStore()
    payload, _golden = _record(
        store,
        result={"text": "stable answer", "usage": {"input_tokens": 1, "output_tokens": 2}},
    )
    seen: list[dict[str, Any]] = []

    def stream(_payload: dict[str, Any]):  # type: ignore[no-untyped-def]
        yield {"delta": {"text": "stable "}}
        yield {
            "result": {
                "text": "stable answer",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        }

    report = Runtime(store).run("revalidation").revalidate_model_call(
        payload,
        fn=stream,
        contract=ReplayContract(provider="mock"),
        on_delta=seen.append,
        keep_chunks=True,
    )
    assert report.matched
    assert len(seen) == 2
    assert "chunks" in store.get(report.live_node_id).result


def test_async_revalidation_has_sync_parity_and_awaits_provider_once() -> None:
    async def exercise() -> None:
        store = MemoryStore()
        payload, golden = _record(store, label="async-revalidation")
        meter = CapturingMeter()
        calls = 0

        async def live(received: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            assert received == payload
            return {
                "id": "async-live",
                "text": "stable answer",
                "usage": {"input_tokens": 4, "output_tokens": 4},
            }

        run = AsyncRuntime(store, meters=[meter]).run("async-revalidation")
        report = await run.arevalidate_model_call(
            payload,
            fn=live,
            contract=ReplayContract(provider="mock", model_revision="async-2"),
            observation_id="async-observation",
        )
        assert calls == 1
        assert report.matched
        assert not report.exact_match
        assert run.cursor_id == golden.id
        assert store.get(report.evidence_node_id).payload["event"] == "model_revalidation"
        assert meter.prechecks == [payload]
        assert meter.charges == [payload]

    asyncio.run(exercise())


def test_async_revalidation_streams_and_replay_mode_never_dispatches() -> None:
    async def exercise() -> None:
        store = MemoryStore()
        payload, _golden = _record(
            store,
            label="async-stream-revalidation",
            result={"text": "async stream", "usage": {"input_tokens": 1, "output_tokens": 2}},
        )
        seen: list[dict[str, Any]] = []

        async def stream(_payload: dict[str, Any]):  # type: ignore[no-untyped-def]
            yield {"delta": {"text": "async "}}
            yield {
                "result": {
                    "text": "async stream",
                    "usage": {"input_tokens": 2, "output_tokens": 2},
                }
            }

        report = await AsyncRuntime(store).run(
            "async-stream-revalidation"
        ).arevalidate_model_call(
            payload,
            fn=stream,
            contract=ReplayContract(provider="mock"),
            on_delta=seen.append,
            keep_chunks=True,
        )
        assert report.matched
        assert len(seen) == 2

        called = False

        async def forbidden(_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal called
            called = True
            return RECORDED_RESULT

        with pytest.raises(RuntimeError, match="record mode"):
            await AsyncRuntime(store, mode="replay").run(
                "async-stream-revalidation"
            ).arevalidate_model_call(
                payload,
                fn=forbidden,
                contract=ReplayContract(provider="mock"),
            )
        assert not called

    asyncio.run(exercise())
