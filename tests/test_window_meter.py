import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from pollard import (
    Budget,
    BudgetExceeded,
    ReservationLeaseLost,
    Runtime,
    SQLiteStore,
    WindowMeter,
)
from pollard.arbiter import BudgetReservation
from pollard.meters import TokenMeter


def test_window_meter_is_shared_across_two_sqlite_writers(tmp_path: Path) -> None:
    path = tmp_path / "window.db"
    with SQLiteStore(path) as store:
        Runtime(store, meters=[WindowMeter("requests", 3, 60)]).run("shared-window")

    barrier = Barrier(2)
    executed: list[tuple[int, int]] = []
    refusals: list[str] = []
    lock = Lock()

    def worker(worker_id: int) -> None:
        with SQLiteStore(path) as store:
            run = Runtime(
                store,
                meters=[WindowMeter("requests", 3, 60)],
            ).run("shared-window")
            barrier.wait()
            for index in range(5):
                try:
                    run.model_call(
                        {"model": "mock", "worker": worker_id, "index": index},
                        attempt=worker_id * 100 + index,
                        fn=lambda _payload, pair=(worker_id, index): _record(
                            executed, lock, pair
                        ),
                    )
                except BudgetExceeded as exc:
                    with lock:
                        refusals.append(exc.refusal_id)
                    break

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, range(2)))

    assert len(executed) == 3
    assert refusals
    with SQLiteStore(path) as store:
        refusal = store.get(refusals[0])
        assert refusal.payload["reason"] == "window"
        assert refusal.payload["meter"] == "requests"
        assert refusal.payload["window_seconds"] == 60


def test_sqlite_budget_reservations_enforce_exact_step_limit(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    with SQLiteStore(path) as store:
        run = Runtime(store).run("reserved-budget", budget=Budget(steps=2))
        run.model_call({"model": "mock", "index": 1}, fn=lambda _payload: {})
        run.model_call({"model": "mock", "index": 2}, fn=lambda _payload: {})
        with pytest.raises(BudgetExceeded):
            run.model_call({"model": "mock", "index": 3}, fn=lambda _payload: {})


def test_expired_sqlite_budget_reservation_releases_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "lease.db"
    clock = iter((100.0, 100.5, 102.0))
    monkeypatch.setattr("pollard.stores.sqlite.time.time", lambda: next(clock))
    request = BudgetReservation(
        scope_id="scope",
        limits={"steps": Decimal("1")},
        baseline={},
        estimates={"steps": Decimal("1")},
    )
    with SQLiteStore(path) as store:
        assert store._pollard_reserve("first", [request], [], 1).ok
        assert not store._pollard_reserve("blocked", [request], [], 1).ok
        assert store._pollard_reserve("after-expiry", [request], [], 1).ok


def test_running_sqlite_call_renews_reservation_past_initial_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "renewed-lease.db"
    started = Event()
    executed: list[str] = []

    def slow_call(_payload: dict[str, object]) -> dict[str, bool]:
        started.set()
        time.sleep(0.5)
        executed.append("first")
        return {"ok": True}

    def first_worker() -> None:
        with SQLiteStore(path) as store:
            run = Runtime(
                store,
                meters=[WindowMeter("requests", 1, 60)],
                reservation_lease_seconds=0.2,
            ).run("sqlite-renewal")
            run.model_call({"model": "slow"}, fn=slow_call)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_worker)
        assert started.wait(timeout=5)
        time.sleep(0.3)
        with SQLiteStore(path) as store:
            second = Runtime(
                store,
                meters=[WindowMeter("requests", 1, 60)],
                reservation_lease_seconds=0.2,
            ).run("sqlite-renewal")
            with pytest.raises(BudgetExceeded):
                second.model_call(
                    {"model": "second"},
                    attempt=1,
                    fn=lambda _payload: executed.append("second") or {"ok": True},
                )
        first.result(timeout=10)

    assert executed == ["first"]


def test_lost_reservation_lease_is_recorded_and_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SQLiteStore(tmp_path / "lost-lease.db") as store:
        monkeypatch.setattr(store, "_pollard_renew", lambda *_args: False)
        run = Runtime(
            store,
            meters=[WindowMeter("requests", 1, 60)],
            reservation_lease_seconds=0.1,
        ).run("lost-lease")
        with pytest.raises(ReservationLeaseLost) as error:
            run.model_call(
                {"model": "slow"},
                fn=lambda _payload: time.sleep(0.2) or {"ok": True},
            )
        node = store.get(error.value.node_id)
        assert node.result == {"ok": True}
        assert node.meta["reservation_lease"]["status"] == "lost"
        assert run.report()["spent"]["requests"] == 1.0


def test_failed_call_releases_window_reservation_immediately(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "failure.db") as store:
        run = Runtime(
            store,
            meters=[WindowMeter("requests", 1, 60)],
        ).run("failure-release")
        with pytest.raises(RuntimeError, match="call failed"):
            run.model_call(
                {"model": "mock", "index": 1},
                fn=lambda _payload: _raise_call_failure(),
            )
        node = run.model_call(
            {"model": "mock", "index": 2},
            fn=lambda _payload: {"ok": True},
        )
        assert node.result == {"ok": True}


def test_estimated_token_budget_records_bounded_settle_overshoot(
    tmp_path: Path,
) -> None:
    class EstimateThree:
        def estimate_input_tokens(self, _payload: dict[str, object]) -> int:
            return 3

    with SQLiteStore(tmp_path / "overshoot.db") as store:
        run = Runtime(
            store,
            meters=[TokenMeter(EstimateThree())],
        ).run("estimated-overshoot", budget=Budget(tokens=4))
        run.model_call(
            {"model": "mock"},
            fn=lambda _payload: {
                "usage": {"input_tokens": 3, "output_tokens": 2}
            },
        )
        assert run.report()["spent"]["tokens"] == 5.0
        with pytest.raises(BudgetExceeded):
            run.model_call(
                {"model": "mock", "index": 2},
                fn=lambda _payload: {},
            )


def _raise_call_failure() -> dict[str, object]:
    raise RuntimeError("call failed")


def _record(
    target: list[tuple[int, int]],
    lock: Lock,
    value: tuple[int, int],
) -> dict[str, object]:
    with lock:
        target.append(value)
    return {"ok": True}
