import time
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime, SQLiteStore
from pollard.arbiter import BudgetReservation, ReservationCheck, WindowReservation
from pollard.errors import PostDispatchOutcomeUnknown, ReservationUncertain
from pollard.meters import StepMeter, TokenMeter
from pollard.runtime import _LeaseHeartbeat


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


class TrackingArbiter(MemoryStore):
    def __init__(self, *, release_error: BaseException | None = None) -> None:
        super().__init__()
        self.release_error = release_error
        self.released: list[str] = []
        self.settled: list[tuple[str, dict[str, Decimal]]] = []

    def _pollard_reserve(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        del budgets, windows, lease_seconds
        return ReservationCheck(ok=True)

    def _pollard_settle(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        self.settled.append((reservation_id, charges))

    def _pollard_release(self, reservation_id: str) -> None:
        self.released.append(reservation_id)
        if self.release_error is not None:
            raise self.release_error

    def _pollard_renew(self, reservation_id: str, lease_seconds: float) -> bool:
        del reservation_id, lease_seconds
        return True


class FailingMeasurement:
    def __init__(self, *, fail_enter: bool = False, fail_exit: bool = False) -> None:
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit
        self.exited = False

    def __enter__(self) -> "FailingMeasurement":
        if self.fail_enter:
            raise RuntimeError("meter enter failed")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.exited = True
        if self.fail_exit:
            raise RuntimeError("meter exit failed")

    def readings(self) -> dict[str, float]:
        return {}


class FailingMeter:
    name = "failing"

    def __init__(self, measurement: FailingMeasurement) -> None:
        self.measurement = measurement

    def measure(self) -> FailingMeasurement:
        return self.measurement

    def charge(
        self,
        node_kind: str,
        payload: dict[str, object],
        result: object,
        meta: dict[str, object],
    ) -> int:
        del node_kind, payload, result, meta
        return 0

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, object],
    ) -> None:
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


def test_on_node_callback_failure_warns_without_breaking_the_run() -> None:
    def broken(_node: object) -> None:
        raise RuntimeError("observer unavailable")

    store = MemoryStore()
    with pytest.warns(RuntimeWarning, match="on_node callback failed"):
        run = Runtime(store, on_node=broken).run("observer-failure")
        node = run.model_call(
            {"model": "mock-1"},
            fn=lambda _payload: {
                "text": "ok",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    assert store.get(node.id).result["text"] == "ok"


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


def test_stream_forwards_chunks_settles_once_and_keeps_chunks() -> None:
    forwarded: list[dict[str, object]] = []

    def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        yield {"delta": {"text": "hel"}}
        yield {"delta": {"text": "lo"}}
        yield {
            "result": {
                "text": "hello",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }
        }

    run = Runtime(MemoryStore()).run("stream")
    node = run.model_call(
        {"model": "mock-1"},
        fn=stream,
        on_delta=forwarded.append,
        keep_chunks=True,
    )

    assert [chunk.get("delta") for chunk in forwarded[:2]] == [
        {"text": "hel"},
        {"text": "lo"},
    ]
    assert node.result["text"] == "hello"
    assert node.result["chunks"] == forwarded
    assert node.meta["charges"]["steps"] == 1
    assert node.meta["charges"]["tokens"] == 5


def test_stream_callback_failure_after_first_chunk_settles_estimate() -> None:
    store = TrackingArbiter()
    run = Runtime(store, meters=[StepMeter()]).run(
        "stream-callback-failure",
        budget=Budget(steps=2),
    )
    callback_error = RuntimeError("consumer stopped")

    def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        yield {"delta": {"text": "started"}}

    with pytest.raises(RuntimeError, match="consumer stopped") as raised:
        run.model_call(
            {"model": "mock"},
            fn=stream,
            on_delta=lambda _chunk: (_ for _ in ()).throw(callback_error),
        )

    assert raised.value is callback_error
    assert store.released == []
    assert store.settled[0][1] == {"steps": Decimal("1")}
    assert run.cursor.payload["event"] == "call_outcome_unknown"


def test_stream_and_non_stream_calls_have_same_identity() -> None:
    payload = {"model": "mock-1", "messages": []}
    direct = Runtime(MemoryStore()).run("identity").model_call(
        payload,
        fn=lambda _payload: {"text": "ok"},
    )

    def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        yield {"result": {"text": "ok"}}

    streamed = Runtime(MemoryStore()).run("identity").model_call(payload, fn=stream)
    assert direct.id == streamed.id


def test_replay_reemits_retained_chunks_without_calling_fn() -> None:
    store = MemoryStore()

    def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        yield {"delta": {"text": "a"}}
        yield {"result": {"text": "a", "usage": {"input_tokens": 1, "output_tokens": 1}}}

    Runtime(store, mode="record").run("stream-replay").model_call(
        {"model": "mock-1"}, fn=stream, keep_chunks=True
    )
    seen: list[dict[str, object]] = []
    replay = Runtime(store, mode="replay").run("stream-replay")
    node = replay.model_call(
        {"model": "mock-1"},
        fn=lambda _payload: (_ for _ in ()).throw(AssertionError("called")),
        on_delta=seen.append,
    )
    assert seen == node.result["chunks"]


def test_estimator_refusal_is_marked_and_fn_is_not_called() -> None:
    class FixedEstimator:
        def estimate_input_tokens(self, payload: dict[str, object]) -> int:
            del payload
            return 10

    called = False

    def fn(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    runtime = Runtime(
        MemoryStore(),
        meters=[StepMeter(), TokenMeter(FixedEstimator(), reserved_output_tokens=5)],
    )
    run = runtime.run("estimated-refusal", budget=Budget(tokens=14))
    with pytest.raises(BudgetExceeded) as exc_info:
        run.model_call({"model": "mock-1"}, fn=fn)
    refusal = run.store.get(exc_info.value.refusal_id)
    assert refusal.payload["estimated"] == "true"
    assert refusal.payload["requested"] == "15"
    assert not called


def test_callable_mutation_cannot_change_recorded_identity_payload() -> None:
    payload = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "original"}],
    }
    expected = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "original"}],
    }
    run = Runtime(MemoryStore()).run("payload-snapshot")

    def mutate(received: dict[str, object]) -> dict[str, object]:
        received.clear()
        received["model"] = "mutated"
        return {"text": "ok"}

    node = run.model_call(payload, fn=mutate)

    assert payload == {"model": "mutated"}
    assert node.payload == expected


def test_meter_enter_failure_stops_lease_and_releases_reservation() -> None:
    store = TrackingArbiter()
    called = False
    meter = FailingMeter(FailingMeasurement(fail_enter=True))
    run = Runtime(store, meters=[StepMeter(), meter]).run(
        "meter-enter",
        budget=Budget(steps=2),
    )

    def call(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(RuntimeError, match="meter enter failed"):
        run.model_call({"model": "mock"}, fn=call)

    assert not called
    assert len(store.released) == 1
    assert store.settled == []


def test_meter_exit_and_release_failures_do_not_mask_callable_error() -> None:
    release_error = ReservationUncertain("release uncertain", "reservation")
    store = TrackingArbiter(release_error=release_error)
    measurement = FailingMeasurement(fail_exit=True)
    run = Runtime(store, meters=[StepMeter(), FailingMeter(measurement)]).run(
        "cleanup-primary",
        budget=Budget(steps=2),
    )
    provider_error = RuntimeError("raw provider detail")

    with pytest.raises(RuntimeError, match="raw provider detail") as raised:
        run.model_call(
            {"model": "mock"},
            fn=lambda _payload: (_ for _ in ()).throw(provider_error),
        )

    assert raised.value is provider_error
    assert measurement.exited
    assert len(store.released) == 1
    assert raised.value.__cause__ is not None
    assert "ReservationUncertain" in str(raised.value.__cause__)


def test_meter_exit_failure_after_result_settles_conservative_estimate() -> None:
    store = TrackingArbiter()
    measurement = FailingMeasurement(fail_exit=True)
    run = Runtime(store, meters=[StepMeter(), FailingMeter(measurement)]).run(
        "post-result-meter-failure",
        budget=Budget(steps=2),
    )

    with pytest.raises(RuntimeError, match="meter exit failed"):
        run.model_call({"model": "mock"}, fn=lambda _payload: {"text": "completed"})

    assert store.released == []
    assert len(store.settled) == 1
    assert store.settled[0][1] == {"steps": Decimal("1")}
    failure = run.cursor
    assert failure.payload["event"] == "call_recording_failed"
    assert failure.meta["failure"] == {
        "outcome": "completed_unrecorded",
        "phase": "post_result_processing",
        "error_type": "RuntimeError",
    }


def test_post_dispatch_unknown_settles_estimates_and_records_safe_note() -> None:
    class EstimateFour:
        def estimate_input_tokens(self, _payload: dict[str, object]) -> int:
            return 4

    store = TrackingArbiter()
    run = Runtime(
        store,
        meters=[StepMeter(), TokenMeter(EstimateFour(), reserved_output_tokens=6)],
    ).run(
        "unknown-outcome",
        budget=Budget(steps=2, tokens=100),
    )
    provider_error = RuntimeError("sensitive provider detail")

    with pytest.raises(RuntimeError, match="sensitive provider detail") as raised:
        run.model_call(
            {"model": "mock", "prompt": "private prompt"},
            fn=lambda _payload: (_ for _ in ()).throw(
                PostDispatchOutcomeUnknown(provider_error)
            ),
        )

    assert raised.value is provider_error
    assert store.released == []
    assert len(store.settled) == 1
    assert store.settled[0][1] == {
        "steps": Decimal("1"),
        "tokens": Decimal("10"),
    }
    failure = run.cursor
    assert failure.kind == "note"
    assert failure.payload["event"] == "call_outcome_unknown"
    assert failure.payload["blocked_kind"] == "model_call"
    assert failure.meta["charges"] == {"steps": 1, "tokens": 10}
    assert failure.meta["failure"] == {
        "outcome": "unknown",
        "phase": "post_dispatch",
        "error_type": "RuntimeError",
    }
    assert "sensitive provider detail" not in str(failure)
    assert "private prompt" not in str(failure)


def test_lease_heartbeat_stop_is_bounded_when_renew_sticks() -> None:
    renewal_started = Event()
    unblock = Event()

    def stuck_renew(_reservation_id: str, _lease_seconds: float) -> bool:
        renewal_started.set()
        unblock.wait(timeout=5)
        return True

    heartbeat = _LeaseHeartbeat(
        reservation_id="stuck-renewal",
        lease_seconds=0.05,
        renew=stuck_renew,
    )
    heartbeat.start()
    assert renewal_started.wait(timeout=1)
    started = time.monotonic()
    detail = heartbeat.stop()
    elapsed = time.monotonic() - started
    unblock.set()

    assert elapsed < 0.5
    assert detail is not None
