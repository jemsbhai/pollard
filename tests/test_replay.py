import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pollard import (
    ActionSpec,
    Budget,
    IntegrityError,
    MemoryStore,
    MissingRecording,
    Registry,
    Runtime,
    SQLiteStore,
)
from pollard.stores.hashrope import HashRopeStore

PAYLOAD = {"model": "mock-1", "messages": [{"role": "user", "content": "hello"}]}
RESULT = {"text": "hello", "usage": {"input_tokens": 2, "output_tokens": 3}}


def test_record_mode_executes_and_records_result_conflicts() -> None:
    store = MemoryStore()
    run = Runtime(store, mode="record").run("conflict")
    first = run.model_call(PAYLOAD, fn=lambda _payload: RESULT)
    run.rollback(run.root_id)
    second = run.model_call(
        PAYLOAD,
        fn=lambda _payload: {"text": "changed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )

    assert second.id == first.id
    assert second.result == RESULT
    conflicts = store.get(first.id).meta["result_conflicts"]
    assert conflicts[0]["result"] == {
        "text": "changed",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_hybrid_hit_serves_recording_and_accounts_avoided_charges() -> None:
    store = MemoryStore()
    Runtime(store, mode="record").run("hybrid").model_call(PAYLOAD, fn=lambda _payload: RESULT)
    run = Runtime(store, mode="hybrid").run("hybrid")
    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": "live"}

    node = run.model_call(PAYLOAD, fn=fn)

    assert not called
    assert node.result == RESULT
    avoided = run.report()["avoided"]
    assert avoided["steps"] == 1.0
    assert avoided["tokens"] == 5.0
    persisted = store.get(node.id).meta["avoided"]
    assert persisted["steps"] == 1
    assert persisted["tokens"] == 5


def test_hybrid_miss_executes_and_stores() -> None:
    store = MemoryStore()
    run = Runtime(store, mode="hybrid").run("hybrid-miss")
    node = run.model_call(PAYLOAD, fn=lambda _payload: RESULT)

    assert node.result == RESULT
    assert store.exists(node.id)
    assert run.report()["avoided"] == {}


def test_replay_hit_never_calls_fn_even_when_budget_would_refuse() -> None:
    store = MemoryStore()
    Runtime(store).run("replay-hit").model_call(PAYLOAD, fn=lambda _payload: RESULT)
    run = Runtime(store, mode="replay").run("replay-hit", budget=Budget(steps=0))

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        raise AssertionError("replay mode called fn")

    node = run.model_call(PAYLOAD, fn=fn)

    assert node.result == RESULT
    assert run.report()["avoided"]["steps"] == 1.0
    assert "avoided" not in store.get(node.id).meta


def test_replay_miss_raises_missing_recording_without_calling_fn() -> None:
    store = MemoryStore()
    Runtime(store).run("replay-miss")
    run = Runtime(store, mode="replay").run("replay-miss")
    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return RESULT

    with pytest.raises(MissingRecording) as exc_info:
        run.model_call(PAYLOAD, fn=fn)

    assert not called
    assert exc_info.value.node_id
    assert "model=mock-1" in exc_info.value.payload_summary
    assert "digest=" in exc_info.value.payload_summary


def test_replay_tool_miss_summary_includes_version_without_calling_fn() -> None:
    store = MemoryStore()
    registry = Registry(
        [
            ActionSpec(
                "lookup",
                "1",
                "Look up a key.",
                {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                False,
                lambda _args: (_ for _ in ()).throw(AssertionError("called")),
            )
        ]
    )
    Runtime(store, registry=registry).run("tool-miss")
    run = Runtime(store, registry=registry, mode="replay").run("tool-miss")

    with pytest.raises(MissingRecording) as exc_info:
        run.tool_call("lookup", {"key": "missing"}, version="1")

    assert "tool=lookup@1" in exc_info.value.payload_summary


def test_runtime_rejects_unknown_replay_mode() -> None:
    with pytest.raises(ValueError, match="record, hybrid, replay"):
        Runtime(mode="unknown")


def test_replay_missing_run_does_not_create_root_or_emit_node() -> None:
    store = MemoryStore()
    emitted: list[object] = []
    runtime = Runtime(store, mode="replay", on_node=emitted.append)

    with pytest.raises(MissingRecording) as exc_info:
        runtime.run("absent")

    assert "root run=absent" in exc_info.value.payload_summary
    assert store.roots() == []
    assert emitted == []


def test_replay_reuses_structural_nodes_without_budget_checks_or_writes() -> None:
    store = MemoryStore()
    recorded = Runtime(store).run("structural")
    first = recorded.note({"stage": "prepared"})
    with recorded.branch(attempt=2) as branch:
        anchor_id = branch.cursor_id
        inside = branch.note({"stage": "candidate"})

    before = list(store.walk(recorded.root_id))
    emitted: list[object] = []
    replay = Runtime(store, mode="replay", on_node=emitted.append).run(
        "structural",
        budget=Budget(steps=0),
    )
    assert replay.note({"stage": "prepared"}).id == first.id
    with replay.branch(attempt=2) as branch:
        assert branch.cursor_id == anchor_id
        assert branch.note({"stage": "candidate"}).id == inside.id
    with pytest.raises(RuntimeError, match="read-only"):
        replay.prune()

    assert list(store.walk(recorded.root_id)) == before
    assert emitted == []


def test_replay_missing_structural_node_fails_without_writing() -> None:
    store = MemoryStore()
    root_id = Runtime(store).run("structural-miss").root_id
    before = list(store.walk(root_id))
    replay = Runtime(store, mode="replay").run("structural-miss")

    with pytest.raises(MissingRecording):
        replay.note({"missing": True})
    with pytest.raises(MissingRecording):
        replay.branch(attempt=4)

    assert list(store.walk(root_id)) == before


def test_replay_does_not_retrofit_registry_binding() -> None:
    store = MemoryStore()
    root_id = Runtime(store).run("unbound").root_id
    registry = Registry(
        [
            ActionSpec(
                "echo",
                "1",
                "Echo text.",
                {"type": "object"},
                False,
                lambda args: {"args": args},
            )
        ]
    )

    with pytest.raises(IntegrityError, match="not bound"):
        Runtime(store, registry=registry, mode="replay").run("unbound")

    assert "registry_digest" not in store.get(root_id).meta


def test_registered_tool_replay_skips_live_handler_and_policy() -> None:
    schema = {
        "type": "object",
        "properties": {
            "token": {"type": "string", "sensitive": True},
            "text": {"type": "string"},
        },
        "required": ["token", "text"],
        "additionalProperties": False,
    }
    recorded_registry = Registry(
        [
            ActionSpec(
                "send",
                "1",
                "Send text.",
                schema,
                True,
                lambda args: {
                    "text": args["text"],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
        ]
    )
    store = MemoryStore()
    expected = Runtime(store, registry=recorded_registry).run("registered-replay").tool_call(
        "send",
        {"token": "secret", "text": "stored"},
    )

    def unavailable_handler(_args: dict[str, object]) -> dict[str, object]:
        raise AssertionError("strict replay called the registered handler")

    replay_registry = Registry(
        [
            ActionSpec(
                "send",
                "1",
                "Send text.",
                schema,
                True,
                unavailable_handler,
            )
        ]
    )

    class UnavailablePolicy:
        def decide(self, _context: object) -> None:
            raise AssertionError("strict replay evaluated a live policy")

    replayed = Runtime(
        store,
        registry=replay_registry,
        policies=[UnavailablePolicy()],  # type: ignore[list-item]
        mode="replay",
    ).run("registered-replay").tool_call(
        "send",
        {"token": "secret", "text": "stored"},
    )
    assert replayed.id == expected.id
    assert replayed.result["text"] == "stored"


def test_strict_replay_does_not_serve_resultless_dry_run_node() -> None:
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    registry = Registry(
        [
            ActionSpec(
                "send",
                "1",
                "Send text.",
                schema,
                True,
                lambda args: {"text": args["text"]},
            )
        ]
    )
    store = MemoryStore()
    preview = Runtime(store, registry=registry, dry_run=True).run("dry-run-replay")
    node = preview.tool_call("send", {"text": "preview"})
    assert node.result is None

    replay = Runtime(store, registry=registry, mode="replay").run("dry-run-replay")
    with pytest.raises(MissingRecording) as exc_info:
        replay.tool_call("send", {"text": "preview"})

    assert "tool=send@1" in exc_info.value.payload_summary


@pytest.mark.parametrize(
    "store_factory",
    [MemoryStore, HashRopeStore],
    ids=["memory", "hashrope"],
)
def test_in_process_replay_results_are_detached_from_stored_state(
    store_factory: Callable[[], Any],
) -> None:
    store = store_factory()
    recorded = Runtime(store).run("detached").model_call(
        PAYLOAD,
        fn=lambda _payload: {
            **RESULT,
            "details": {"values": ["stored"]},
        },
    )
    recorded.payload["model"] = "mutated"
    recorded.result["details"]["values"][0] = "mutated"
    recorded.meta["charges"]["steps"] = 999

    stored = store.get(recorded.id)
    assert stored.payload["model"] == "mock-1"
    assert stored.result["details"]["values"] == ["stored"]
    assert stored.meta["charges"]["steps"] == 1

    replayed = Runtime(store, mode="replay").run("detached").model_call(
        PAYLOAD,
        fn=lambda _payload: (_ for _ in ()).throw(AssertionError("called")),
    )
    replayed.result["details"]["values"][0] = "changed again"
    replayed_again = Runtime(store, mode="replay").run("detached").model_call(
        PAYLOAD,
        fn=lambda _payload: (_ for _ in ()).throw(AssertionError("called")),
    )
    assert replayed_again.result["details"]["values"] == ["stored"]


def test_sqlite_path_replay_is_physically_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "read-only.db"
    with SQLiteStore(db_path) as store:
        Runtime(store).run("read-only").model_call(
            PAYLOAD,
            fn=lambda _payload: RESULT,
        )
    before = db_path.read_bytes()

    runtime = Runtime(db_path, mode="replay")
    assert isinstance(runtime.store, SQLiteStore)
    assert runtime.store.read_only is True
    try:
        replay = runtime.run("read-only")
        node = replay.model_call(
            PAYLOAD,
            fn=lambda _payload: (_ for _ in ()).throw(AssertionError("called")),
        )
        assert node.result == RESULT
        with pytest.raises(RuntimeError, match="read-only"):
            replay.prune()
    finally:
        runtime.store.close()

    assert db_path.read_bytes() == before


def test_sqlite_path_replay_does_not_create_missing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    runtime = Runtime(db_path, mode="replay")

    with pytest.raises(MissingRecording):
        runtime.run("missing")

    assert not db_path.exists()


def test_replay_verifies_recording_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "recording.db"
    run = Runtime(db_path).run("tamper")
    run.model_call(PAYLOAD, fn=lambda _payload: RESULT)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET result_digest = ? WHERE kind = ?",
            ("0" * 64, "model_call"),
        )

    replay = Runtime(db_path, mode="replay").run("tamper")
    with pytest.raises(IntegrityError, match="integrity"):
        replay.model_call(PAYLOAD, fn=lambda _payload: RESULT)


def test_replay_reports_missing_or_changed_ancestor_as_integrity_error(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "parent-tamper.db"
    with SQLiteStore(db_path) as store:
        Runtime(store).run("parent-tamper").model_call(
            PAYLOAD,
            fn=lambda _payload: RESULT,
        )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET parent = ? WHERE kind = ?",
            ("f" * 64, "model_call"),
        )

    runtime = Runtime(db_path, mode="replay")
    try:
        replay = runtime.run("parent-tamper")
        with pytest.raises(IntegrityError, match="node is missing"):
            replay.model_call(PAYLOAD, fn=lambda _payload: RESULT)
    finally:
        assert isinstance(runtime.store, SQLiteStore)
        runtime.store.close()
