import sqlite3
from pathlib import Path

import pytest

from pollard import Budget, IntegrityError, MemoryStore, MissingRecording, Runtime

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
    run = Runtime(MemoryStore(), mode="replay").run("replay-miss")
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
