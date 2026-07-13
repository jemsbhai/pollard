from pathlib import Path

import pytest

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime, SQLiteStore


class FakeMeasurement:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeMeasurement":
        self.entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.exited = True

    def readings(self) -> dict[str, float]:
        return {"joules": 2.5}


class FakeJouleMeter:
    name = "joules"

    def __init__(self) -> None:
        self.measurement = FakeMeasurement()

    def measure(self) -> FakeMeasurement:
        return self.measurement

    def charge(
        self,
        node_kind: str,
        payload: dict[str, object],
        result: object,
        meta: dict[str, object],
    ) -> float:
        del node_kind, payload, result
        value = meta.get("joules", 0.0)
        return float(value)

    def precheck_estimate(self, node_kind: str, payload: dict[str, object]) -> None:
        del node_kind, payload
        return None


def test_model_call_records_result_charges_and_moves_cursor() -> None:
    with Runtime(MemoryStore()).run("model") as run:
        node = run.model_call(
            {"model": "mock-1"},
            fn=lambda _payload: {
                "text": "ok",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )
    assert run.cursor_id == node.id
    assert node.result == {"text": "ok", "usage": {"input_tokens": 2, "output_tokens": 3}}
    assert node.meta["charges"]["steps"] == 1
    assert node.meta["charges"]["tokens"] == 5


def test_runtime_records_measurement_meter_readings() -> None:
    meter = FakeJouleMeter()
    run = Runtime(MemoryStore(), meters=[meter]).run("measure")
    node = run.model_call({"model": "mock-1"}, fn=lambda _payload: {"text": "ok"})
    assert meter.measurement.entered
    assert meter.measurement.exited
    assert node.meta["joules"] == 2.5
    assert node.meta["charges"]["joules"] == 2.5


def test_tool_call_wraps_name_and_args_in_payload() -> None:
    run = Runtime(MemoryStore()).run("tool")

    def echo(payload: dict[str, object]) -> dict[str, object]:
        return {"seen": payload, "usage": {"input_tokens": 0, "output_tokens": 0}}

    node = run.tool_call("judge", {"text": "hello"}, fn=echo)
    assert node.payload == {"tool": "judge", "args": {"text": "hello"}}
    assert node.result == {
        "seen": {"tool": "judge", "args": {"text": "hello"}},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def test_branch_has_isolated_cursor() -> None:
    run = Runtime(MemoryStore()).run("branch")
    original = run.cursor_id
    with run.branch(attempt=2) as branch:
        branch.model_call(
            {"model": "mock-1"},
            fn=lambda _payload: {
                "text": "branch",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )
        branch_tip = branch.cursor_id

    assert run.cursor_id == original
    assert branch_tip != original
    assert len(run.store.children(original)) == 1


def test_rollback_moves_to_an_ancestor_and_continuation_branches() -> None:
    run = Runtime(MemoryStore()).run("rollback")
    first = run.model_call(
        {"model": "mock-1", "n": 1},
        fn=lambda _payload: {"text": "one", "usage": {"input_tokens": 0, "output_tokens": 0}},
    )
    second = run.model_call(
        {"model": "mock-1", "n": 2},
        fn=lambda _payload: {"text": "two", "usage": {"input_tokens": 0, "output_tokens": 0}},
    )
    assert run.cursor_id == second.id

    run.rollback(steps=1)
    assert run.cursor_id == first.id
    note = run.note({"checkpoint": "after rollback"})
    assert note.parent == first.id


def test_rollback_rejects_non_ancestor() -> None:
    run = Runtime(MemoryStore()).run("rollback-bad")
    other = Runtime(MemoryStore()).run("other").root_id
    with pytest.raises(ValueError):
        run.rollback(other)


def test_resume_uses_deepest_non_pruned_leaf(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    with SQLiteStore(db_path) as store:
        run = Runtime(store).run("resume")
        first = run.model_call(
            {"model": "mock-1", "n": 1},
            fn=lambda _payload: {"text": "one", "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        second = run.model_call(
            {"model": "mock-1", "n": 2},
            fn=lambda _payload: {"text": "two", "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        assert first.parent == run.root_id
        assert second.parent == first.id

    with SQLiteStore(db_path) as store:
        resumed = Runtime(store).resume("resume")
        assert resumed.cursor_id == second.id


def test_resume_ignores_pruned_tip(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    with SQLiteStore(db_path) as store:
        run = Runtime(store).run("resume-prune")
        first = run.model_call(
            {"model": "mock-1", "n": 1},
            fn=lambda _payload: {"text": "one", "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        run.model_call(
            {"model": "mock-1", "n": 2},
            fn=lambda _payload: {"text": "two", "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        run.prune()

    with SQLiteStore(db_path) as store:
        resumed = Runtime(store).resume("resume-prune")
        assert resumed.cursor_id == first.id


def test_same_label_and_attempt_use_same_root() -> None:
    runtime = Runtime(MemoryStore())
    first = runtime.run("same")
    second = runtime.run("same")
    fresh = runtime.run("same", attempt=1)
    assert first.root_id == second.root_id
    assert fresh.root_id != first.root_id


def test_branch_budget_refuses_inside_branch_without_moving_parent() -> None:
    run = Runtime(MemoryStore()).run("branch-budget")
    original = run.cursor_id
    with run.branch(attempt=1, budget=Budget(steps=0)) as branch, pytest.raises(BudgetExceeded):
        branch.model_call({"model": "mock-1"}, fn=lambda _payload: {"text": "x"})
    assert run.cursor_id == original
