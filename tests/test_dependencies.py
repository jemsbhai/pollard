from dataclasses import replace

import pytest

from pollard._canon import IdentityValue
from pollard.dependencies import (
    ResultReference,
    record_dependency,
    record_handoff,
    verify_dependencies,
)
from pollard.errors import IntegrityError, MissingRecording
from pollard.meters import StepMeter
from pollard.runtime import Runtime
from pollard.store import MemoryStore
from pollard.tree import Node
from pollard.verify import verify


def test_cross_branch_dependencies_bind_results_and_replay_without_dispatch() -> None:
    store = MemoryStore()

    def execute(mode: str) -> tuple[str, str]:
        runtime = Runtime(store, mode=mode, meters=[StepMeter()])
        run = runtime.run("mission")
        with run.branch(attempt=1) as researcher:
            def model(_: object) -> dict[str, str]:
                assert mode == "record", "replay dispatched a model"
                return {"text": "source evidence"}

            source = researcher.model_call({"model": "mock"}, fn=model)
            handoff = record_handoff(
                researcher, [ResultReference.from_node(source)],
                recipient="reviewer", task_id="check",
            )
        with run.branch(attempt=2) as reviewer:
            dependency = record_dependency(reviewer, [ResultReference.from_node(source)])
        return handoff.id, dependency.id

    recorded = execute("record")
    count = len(store._nodes)
    assert execute("replay") == recorded
    assert len(store._nodes) == count
    root = store.roots()[0]
    report = verify_dependencies(store, root)
    assert report.ok
    assert report.checked_notes == 2
    assert report.references == 2
    assert report.to_dict()["ok"] is True


def test_changed_valid_result_is_detected_even_when_node_verification_passes() -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    source = run.model_call({}, fn=lambda _: {"answer": "old"})
    record_dependency(run, [ResultReference.from_node(source)])
    replacement = Node.make(
        kind=source.kind, parent=source.parent, payload=source.payload, result={"answer": "new"}
    )
    assert replacement.id == source.id
    store = runtime.store
    assert isinstance(store, MemoryStore)
    store._nodes[source.id] = replacement
    assert verify(store, source.id).ok
    report = verify_dependencies(store, run.root_id)
    assert not report.ok
    assert report.findings[0].code == "changed_result"
    assert report.findings[0].target_id == source.id


def test_recording_refuses_missing_changed_and_cross_run_references_before_note() -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    other = runtime.run("other")
    source = other.tool_call("lookup", {}, fn=lambda _: {"data": 1})
    cursor = run.cursor_id
    with pytest.raises(IntegrityError, match="cross_run_reference"):
        record_dependency(run, [ResultReference.from_node(source)])
    with pytest.raises(IntegrityError, match="missing_target"):
        record_dependency(run, [ResultReference("0" * 64, "1" * 64)])
    own = run.model_call({}, fn=lambda _: {"text": "ok"})
    with pytest.raises(IntegrityError, match="changed_result"):
        record_dependency(run, [ResultReference(own.id, "0" * 64)])
    assert run.cursor_id == own.id
    assert cursor == run.root_id


@pytest.mark.parametrize("case", ["missing", "cross_run", "no_result", "malformed"])
def test_offline_verification_detects_invalid_references(case: str) -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    other = runtime.run("other")
    target = other.model_call({}, fn=lambda _: {"text": "other"})
    refs: IdentityValue = [{"node_id": target.id, "result_digest": target.result_digest}]
    if case == "missing":
        refs = [{"node_id": "0" * 64, "result_digest": "1" * 64}]
    elif case == "no_result":
        refs = [{"node_id": run.root_id, "result_digest": "1" * 64}]
    elif case == "malformed":
        refs = [{"node_id": target.id}]
    run.note({"_pollard": {"dependency": {
        "version": 1, "relation": "consumes", "references": refs,
    }}})
    report = verify_dependencies(runtime.store, run.root_id)
    expected = {
        "missing": "missing_target", "cross_run": "cross_run_reference",
        "no_result": "missing_result", "malformed": "malformed_reference",
    }
    assert [item.code for item in report.findings] == [expected[case]]


def test_tampered_note_and_target_ancestry_are_rejected() -> None:
    store = MemoryStore()
    runtime = Runtime(store, meters=[StepMeter()])
    run = runtime.run("mission")
    source = run.model_call({}, fn=lambda _: {"text": "ok"})
    note = record_dependency(run, [ResultReference.from_node(source)])
    store._nodes[source.id] = replace(source, payload={"tampered": True})
    report = verify_dependencies(store, run.root_id)
    assert {item.code for item in report.findings} == {"invalid_note", "invalid_target"}
    store._nodes[source.id] = source
    store._nodes[note.id] = replace(note, payload={**note.payload, "tampered": True})
    assert verify_dependencies(store, run.root_id).findings[0].code == "invalid_note"


def test_reference_creation_rejects_notes_and_invalid_result_digest() -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    with pytest.raises(ValueError, match="completed"):
        ResultReference.from_node(run.note({"message": "no result"}))
    source = run.model_call({}, fn=lambda _: {"text": "ok"})
    with pytest.raises(IntegrityError, match="result digest"):
        ResultReference.from_node(replace(source, result_digest="0" * 64))
    with pytest.raises(ValueError, match="node_id"):
        ResultReference("not a node", "0" * 64)


def test_invalid_documents_and_missing_replay_note_do_not_mutate_cursor() -> None:
    store = MemoryStore()
    runtime = Runtime(store, meters=[StepMeter()])
    run = runtime.run("mission")
    source = run.model_call({}, fn=lambda _: {"text": "ok"})
    reference = ResultReference.from_node(source)
    with pytest.raises(ValueError, match="at least one"):
        record_dependency(run, [])
    with pytest.raises(ValueError, match="recipient"):
        record_handoff(run, [reference], recipient="")
    with pytest.raises(TypeError, match="ResultReference"):
        record_dependency(run, [source])  # type: ignore[list-item]
    assert run.cursor_id == source.id
    replay = Runtime(store, mode="replay", meters=[StepMeter()]).run("mission")
    replay.model_call({}, fn=lambda _: pytest.fail("must not execute"))
    with pytest.raises(MissingRecording):
        record_dependency(replay, [reference])
    assert replay.cursor_id == source.id


def test_reference_order_and_duplicates_have_one_canonical_note_identity() -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    first = run.model_call({"call": 1}, fn=lambda _: {"text": "one"})
    second = run.model_call({"call": 2}, fn=lambda _: {"text": "two"})
    a, b = ResultReference.from_node(first), ResultReference.from_node(second)
    note = record_dependency(run, [b, a, b])
    run.rollback(second.id)
    assert record_dependency(run, [a, b]).id == note.id
    assert verify_dependencies(runtime.store, run.root_id).references == 2


def test_dependency_marker_on_call_and_unknown_version_are_malformed() -> None:
    runtime = Runtime(meters=[StepMeter()])
    run = runtime.run("mission")
    run.note({"_pollard": {"dependency": {"version": True}}})
    run.model_call({"_pollard": {"dependency": {"version": 1}}}, fn=lambda _: {})
    assert all(
        item.code == "malformed_reference"
        for item in verify_dependencies(runtime.store, run.root_id).findings
    )
