import importlib
import json
import multiprocessing
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from threading import Barrier, Event, Lock
from typing import Any
from uuid import uuid4

import pytest

from pollard import (
    Budget,
    BudgetExceeded,
    PostgresStore,
    Runtime,
    WindowMeter,
)
from pollard.arbiter import BudgetReservation, WindowReservation
from pollard.cli import main
from pollard.errors import IntegrityError, ReservationUncertain, SettlementUncertain
from pollard.tree import Node, NodeKind


def test_postgres_store_is_lazy_optional_import() -> None:
    module = importlib.import_module("pollard.stores.postgres")
    assert module.PostgresStore is PostgresStore


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_fresh_schema_records_current_version() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        with PostgresStore(isolated_dsn):
            pass
        assert _schema_version(isolated_dsn) == 2


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_legacy_schema_requires_explicit_forward_migration() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        _create_legacy_schema(isolated_dsn)
        legacy_root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "legacy-postgres"},
        )
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO pollard_nodes
                  (store_id, id, parent, kind, attempt, payload,
                   result, result_digest, meta)
                VALUES ('default', %s, NULL, %s, %s, %s, NULL, NULL, '{}')
                """,
                (
                    legacy_root.id,
                    legacy_root.kind,
                    legacy_root.attempt,
                    json.dumps(legacy_root.payload, sort_keys=True, separators=(",", ":")),
                ),
            )
        with pytest.raises(IntegrityError, match="migration required"):
            PostgresStore(isolated_dsn)

        assert PostgresStore.migrate(isolated_dsn) == (0, 2)
        with PostgresStore(isolated_dsn) as store:
            assert store.get(legacy_root.id) == legacy_root
            Runtime(store).run("migrated")
        assert _schema_version(isolated_dsn) == 2
        assert PostgresStore.migrate(isolated_dsn) == (2, 2)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_migration_refuses_in_flight_legacy_reservation() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        _create_legacy_schema(isolated_dsn)
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO pollard_reservations
                  (store_id, reservation_id, kind, scope_id, meter,
                   amount, expires_at, window_seconds)
                VALUES ('default', 'in-flight', 'budget', 'scope',
                        'steps', 1, 9999999999, NULL)
                """
            )
        with pytest.raises(IntegrityError, match="empty reservation table"):
            PostgresStore.migrate(isolated_dsn)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_unknown_schema_version_is_refused() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        with PostgresStore(isolated_dsn):
            pass
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            conn.execute("UPDATE pollard_schema SET version = 999 WHERE singleton = 1")
        with pytest.raises(IntegrityError, match="unsupported PostgreSQL schema version: 999"):
            PostgresStore(isolated_dsn)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_partial_unversioned_schema_is_refused() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            conn.execute(
                """
                CREATE TABLE pollard_nodes (
                  store_id TEXT NOT NULL, id TEXT NOT NULL,
                  PRIMARY KEY (store_id, id)
                )
                """
            )
        with pytest.raises(IntegrityError, match="unsupported unversioned"):
            PostgresStore(isolated_dsn)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_changed_current_table_layout_is_refused() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with _isolated_schema_dsn(dsn) as isolated_dsn:
        with PostgresStore(isolated_dsn):
            pass
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(isolated_dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE pollard_nodes ADD COLUMN unexpected TEXT")
        with pytest.raises(IntegrityError, match="unsupported PostgreSQL table layout"):
            PostgresStore(isolated_dsn)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_logical_stores_are_isolated_and_intern_payloads() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    first_id = f"first-{uuid4().hex}"
    second_id = f"second-{uuid4().hex}"
    with (
        PostgresStore(dsn, store_id=first_id, intern_threshold=32) as first,
        PostgresStore(dsn, store_id=second_id, intern_threshold=32) as second,
    ):
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "pg", "body": "x" * 256},
        )
        first.put(root)
        assert first.get(root.id).payload == root.payload
        assert not second.exists(root.id)


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_window_meter_is_atomic_across_two_threads() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"window-{uuid4().hex}"
    label = f"window-{uuid4().hex}"
    with PostgresStore(dsn, store_id=store_id) as store:
        Runtime(store, meters=[WindowMeter("requests", 3, 60)]).run(label)
    barrier = Barrier(2)
    lock = Lock()
    executed: list[tuple[int, int]] = []

    def worker(worker_id: int) -> None:
        with PostgresStore(dsn, store_id=store_id) as store:
            run = Runtime(
                store,
                meters=[WindowMeter("requests", 3, 60)],
            ).run(label)
            barrier.wait()
            for index in range(5):
                try:
                    run.model_call(
                        {"model": "mock", "worker": worker_id, "index": index},
                        attempt=worker_id * 100 + index,
                        fn=lambda _payload, value=(worker_id, index): _thread_result(
                            executed, lock, value
                        ),
                    )
                except BudgetExceeded:
                    break

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, range(2)))
    assert len(executed) == 3


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
@pytest.mark.parametrize(
    ("lease_seconds", "call_seconds"),
    [(0.5, 0.1), (0.6, 1.4)],
    ids=["duration_below_lease", "duration_above_lease"],
)
def test_postgres_running_call_keeps_exact_window_reservation(
    lease_seconds: float,
    call_seconds: float,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"lease-{uuid4().hex}"
    label = f"lease-{uuid4().hex}"
    started = Event()
    executed: list[str] = []

    def slow_call(_payload: dict[str, object]) -> dict[str, bool]:
        started.set()
        time.sleep(call_seconds)
        executed.append("first")
        return {"ok": True}

    def first_worker() -> None:
        with PostgresStore(dsn, store_id=store_id) as store:
            run = Runtime(
                store,
                meters=[WindowMeter("requests", 1, 60)],
                reservation_lease_seconds=lease_seconds,
            ).run(label)
            run.model_call({"model": "slow"}, fn=slow_call)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_worker)
        assert started.wait(timeout=5)
        time.sleep(min(call_seconds / 2, lease_seconds * 1.25))
        with PostgresStore(dsn, store_id=store_id) as store:
            second = Runtime(
                store,
                meters=[WindowMeter("requests", 1, 60)],
                reservation_lease_seconds=lease_seconds,
            ).run(label)
            with pytest.raises(BudgetExceeded):
                second.model_call(
                    {"model": "second"},
                    attempt=1,
                    fn=lambda _payload: executed.append("second") or {"ok": True},
                )
        first.result(timeout=10)

    assert executed == ["first"]


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_reserve_and_settle_retries_are_idempotent() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    request = BudgetReservation(
        scope_id=f"scope-{uuid4().hex}",
        limits={"steps": Decimal("2")},
        baseline={},
        estimates={"steps": Decimal("1")},
    )
    reservation_id = uuid4().hex
    with PostgresStore(dsn, store_id=f"idempotent-{uuid4().hex}") as store:
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        store._pollard_settle(reservation_id, {"steps": Decimal("1")})
        store._pollard_settle(reservation_id, {"steps": Decimal("1")})
        settled = store._conn.execute(
            """
            SELECT settled FROM pollard_budget_state
            WHERE store_id = %s AND scope_id = %s AND meter = 'steps'
            """,
            (store.store_id, request.scope_id),
        ).fetchone()
        assert settled is not None
        assert Decimal(str(settled[0])) == Decimal("1")
        with pytest.raises(IntegrityError, match="different charges"):
            store._pollard_settle(reservation_id, {"steps": Decimal("2")})


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_reservation_lease_starts_after_budget_row_lock_wait() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"reserve-lock-time-{uuid4().hex}"
    request = _step_reservation()
    reservation_id = uuid4().hex
    lease_seconds = 1.0
    with PostgresStore(dsn, store_id=store_id) as store:
        assert store._pollard_reserve("seed", [request], [], 60).ok
        store._pollard_release("seed")
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(dsn) as blocker:
            blocker.execute(
                """
                SELECT settled FROM pollard_budget_state
                WHERE store_id = %s AND scope_id = %s AND meter = 'steps'
                FOR UPDATE
                """,
                (store_id, request.scope_id),
            ).fetchone()
            ready = Event()
            start = Event()
            backend_pid: list[int] = []

            def reserve() -> bool:
                with PostgresStore(dsn, store_id=store_id) as worker:
                    backend_pid.append(int(worker._conn.info.backend_pid))
                    ready.set()
                    assert start.wait(timeout=5)
                    return worker._pollard_reserve(
                        reservation_id, [request], [], lease_seconds
                    ).ok

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(reserve)
                assert ready.wait(timeout=5)
                start.set()
                _wait_for_postgres_lock(store._conn, backend_pid[0])
                time.sleep(0.1)
                released_at = store._database_time_for(blocker)
                blocker.commit()
                assert future.result(timeout=5)

        row = store._conn.execute(
            """
            SELECT expires_at FROM pollard_reservation_state
            WHERE store_id = %s AND reservation_id = %s
            """,
            (store_id, reservation_id),
        ).fetchone()
        assert row is not None
        assert float(row[0]) >= released_at + lease_seconds - 0.05


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_window_settlement_time_is_after_row_lock_wait() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"settle-lock-time-{uuid4().hex}"
    reservation_id = uuid4().hex
    request = WindowReservation(
        ledger_key=f"window-{uuid4().hex}",
        meter="requests",
        limit=Decimal("1"),
        amount=Decimal("1"),
        window_seconds=60,
    )
    with PostgresStore(dsn, store_id=store_id) as store:
        assert store._pollard_reserve(reservation_id, [], [request], 60).ok
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(dsn) as blocker:
            blocker.execute(
                """
                SELECT state FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (store_id, reservation_id),
            ).fetchone()
            ready = Event()
            start = Event()
            backend_pid: list[int] = []

            def settle() -> None:
                with PostgresStore(dsn, store_id=store_id) as worker:
                    backend_pid.append(int(worker._conn.info.backend_pid))
                    ready.set()
                    assert start.wait(timeout=5)
                    worker._pollard_settle(
                        reservation_id, {"requests": Decimal("1")}
                    )

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(settle)
                assert ready.wait(timeout=5)
                start.set()
                _wait_for_postgres_lock(store._conn, backend_pid[0])
                time.sleep(0.1)
                released_at = store._database_time_for(blocker)
                blocker.commit()
                future.result(timeout=5)

        row = store._conn.execute(
            """
            SELECT settled_at FROM pollard_window_events
            WHERE store_id = %s AND scope_id = %s
            """,
            (store_id, request.ledger_key),
        ).fetchone()
        assert row is not None
        assert float(row[0]) >= released_at


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_renewal_lease_starts_after_reservation_row_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"renew-lock-time-{uuid4().hex}"
    reservation_id = uuid4().hex
    request = _step_reservation()
    lease_seconds = 1.0
    with PostgresStore(dsn, store_id=store_id) as store:
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        original_connect = store._psycopg.connect
        with original_connect(dsn) as blocker:
            blocker.execute(
                """
                SELECT state FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (store_id, reservation_id),
            ).fetchone()
            connected = Event()
            backend_pid: list[int] = []

            def capture_connection(*args: Any, **kwargs: Any) -> Any:
                conn = original_connect(*args, **kwargs)
                backend_pid.append(int(conn.info.backend_pid))
                connected.set()
                return conn

            monkeypatch.setattr(store._psycopg, "connect", capture_connection)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    store._pollard_renew, reservation_id, lease_seconds
                )
                assert connected.wait(timeout=5)
                _wait_for_postgres_lock(store._conn, backend_pid[0])
                time.sleep(0.1)
                released_at = store._database_time_for(blocker)
                blocker.commit()
                assert future.result(timeout=5)

        row = store._conn.execute(
            """
            SELECT expires_at FROM pollard_reservation_state
            WHERE store_id = %s AND reservation_id = %s
            """,
            (store_id, reservation_id),
        ).fetchone()
        assert row is not None
        assert float(row[0]) >= released_at + lease_seconds - 0.05


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_reconnects_after_backend_loss() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    with PostgresStore(dsn, store_id=f"reconnect-{uuid4().hex}") as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "reconnect"})
        store.put(root)
        backend_pid = store._conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(dsn, autocommit=True) as killer:
            assert killer.execute(
                "SELECT pg_terminate_backend(%s)", (backend_pid,)
            ).fetchone()[0]
        store.reconnect()
        assert store.get(root.id) == root


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_reserve_commit_with_lost_ack_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    request = _step_reservation()
    reservation_id = uuid4().hex
    with PostgresStore(dsn, store_id=f"reserve-ack-{uuid4().hex}") as store:
        original = store._pollard_reserve_once
        first = True

        def lose_first_ack(*args: Any, **kwargs: Any) -> Any:
            nonlocal first
            result = original(*args, **kwargs)
            if first:
                first = False
                store._conn.close()
                raise store._psycopg.OperationalError("reserve acknowledgement lost")
            return result

        monkeypatch.setattr(store, "_pollard_reserve_once", lose_first_ack)
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        count = store._conn.execute(
            """
            SELECT COUNT(*) FROM pollard_reservation_state
            WHERE store_id = %s AND reservation_id = %s
            """,
            (store.store_id, reservation_id),
        ).fetchone()
        assert count is not None and int(count[0]) == 1


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_settle_commit_with_lost_ack_is_recovered_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    request = _step_reservation()
    reservation_id = uuid4().hex
    with PostgresStore(dsn, store_id=f"settle-ack-{uuid4().hex}") as store:
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        original = store._pollard_settle_once
        first = True

        def lose_first_ack(*args: Any, **kwargs: Any) -> None:
            nonlocal first
            original(*args, **kwargs)
            if first:
                first = False
                store._conn.close()
                raise store._psycopg.OperationalError("settle acknowledgement lost")

        monkeypatch.setattr(store, "_pollard_settle_once", lose_first_ack)
        store._pollard_settle(reservation_id, {"steps": Decimal("1")})
        settled = store._conn.execute(
            """
            SELECT settled FROM pollard_budget_state
            WHERE store_id = %s AND scope_id = %s AND meter = 'steps'
            """,
            (store.store_id, request.scope_id),
        ).fetchone()
        assert settled is not None and Decimal(str(settled[0])) == Decimal("1")


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_release_commit_with_lost_ack_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    request = _step_reservation()
    reservation_id = uuid4().hex
    with PostgresStore(dsn, store_id=f"release-ack-{uuid4().hex}") as store:
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        original = store._pollard_release_once
        first = True

        def lose_first_ack(*args: Any, **kwargs: Any) -> None:
            nonlocal first
            original(*args, **kwargs)
            if first:
                first = False
                store._conn.close()
                raise store._psycopg.OperationalError("release acknowledgement lost")

        monkeypatch.setattr(store, "_pollard_release_once", lose_first_ack)
        store._pollard_release(reservation_id)
        state = store._conn.execute(
            """
            SELECT state FROM pollard_reservation_state
            WHERE store_id = %s AND reservation_id = %s
            """,
            (store.store_id, reservation_id),
        ).fetchone()
        details = store._conn.execute(
            """
            SELECT COUNT(*) FROM pollard_reservations
            WHERE store_id = %s AND reservation_id = %s
            """,
            (store.store_id, reservation_id),
        ).fetchone()
        assert state is not None and str(state[0]) == "released"
        assert details is not None and int(details[0]) == 0


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_completed_provider_call_recovers_settlement_after_connection_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    executed: list[str] = []
    with PostgresStore(dsn, store_id=f"provider-loss-{uuid4().hex}") as store:
        original = store._pollard_settle_once
        first = True

        def lose_first_ack(*args: Any, **kwargs: Any) -> None:
            nonlocal first
            original(*args, **kwargs)
            if first:
                first = False
                store._conn.close()
                raise store._psycopg.OperationalError("database lost after provider")

        monkeypatch.setattr(store, "_pollard_settle_once", lose_first_ack)
        run = Runtime(
            store,
            meters=[WindowMeter("requests", 1, 60)],
        ).run(f"provider-loss-{uuid4().hex}")
        node = run.model_call(
            {"model": "mock"},
            fn=lambda _payload: executed.append("provider") or {"ok": True},
        )
        assert node.result == {"ok": True}
        assert executed == ["provider"]
        assert run.report()["spent"]["requests"] == 1.0


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_persistent_reserve_settle_and_release_failures_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    request = _step_reservation()
    with PostgresStore(dsn, store_id=f"uncertain-{uuid4().hex}") as store:
        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise store._psycopg.OperationalError("database unavailable")

        monkeypatch.setattr(store, "_pollard_reserve_once", unavailable)
        reservation_id = uuid4().hex
        with pytest.raises(ReservationUncertain) as reserve_error:
            store._pollard_reserve(reservation_id, [request], [], 60)
        assert reserve_error.value.reservation_id == reservation_id

        monkeypatch.undo()
        assert store._pollard_reserve(reservation_id, [request], [], 60).ok
        monkeypatch.setattr(store, "_pollard_settle_once", unavailable)
        with pytest.raises(SettlementUncertain) as settle_error:
            store._pollard_settle(reservation_id, {"steps": Decimal("1")})
        assert settle_error.value.reservation_id == reservation_id

        monkeypatch.setattr(store, "_pollard_release_once", unavailable)
        with pytest.raises(ReservationUncertain) as release_error:
            store._pollard_release(reservation_id)
        assert release_error.value.reservation_id == reservation_id


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_window_settlement_cannot_escape_eight_writer_limit() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    for _round in range(5):
        assert _run_eight_writer_window_round(dsn) == 16


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_concurrent_puts_are_benign_and_meta_patches_are_not_lost() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"put-{uuid4().hex}"
    with PostgresStore(dsn, store_id=store_id) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "put-race"})
        store.put(root)
        child = Node.make(
            kind=NodeKind.NOTE,
            parent=root.id,
            payload={"same": True},
        )
    barrier = Barrier(2)

    def worker(worker_id: int) -> None:
        with PostgresStore(dsn, store_id=store_id) as store:
            barrier.wait()
            store.put(child)
            store.update_meta(root.id, {f"worker_{worker_id}": True})

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, range(2)))
    with PostgresStore(dsn, store_id=store_id) as store:
        assert store.get(child.id) == child
        assert store.get(root.id).meta == {"worker_0": True, "worker_1": True}


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_budget_contention_two_processes_twenty_rounds() -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    context = multiprocessing.get_context("spawn")
    step_limit = 4
    for round_index in range(20):
        store_id = f"process-{uuid4().hex}"
        label = f"contention-{round_index}-{uuid4().hex}"
        with PostgresStore(dsn, store_id=store_id) as store:
            Runtime(store).run(label, budget=Budget(steps=step_limit))
        start = context.Event()
        queue = context.Queue()
        processes = [
            context.Process(
                target=_process_budget_worker,
                args=(
                    dsn,
                    store_id,
                    label,
                    worker_id,
                    step_limit,
                    start,
                    queue,
                ),
            )
            for worker_id in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        executed = sum(queue.get(timeout=30) for _ in processes)
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert executed == step_limit, f"contention round {round_index}"


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_POSTGRES_DSN"),
    reason="POLLARD_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_cli_uses_env_spec_without_printing_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
    store_id = f"cli-{uuid4().hex}"
    with PostgresStore(dsn, store_id=store_id) as store:
        Runtime(store).run("postgres-cli")
    monkeypatch.setenv("POLLARD_CLI_PG_DSN", dsn)

    assert main(["runs", f"pg-env:POLLARD_CLI_PG_DSN#{store_id}", "--json"]) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["runs"][0]["label"] == "postgres-cli"
    assert document["runs"][0]["store"] == f"pg-env:POLLARD_CLI_PG_DSN#{store_id}"
    assert dsn not in output


def _process_budget_worker(
    dsn: str,
    store_id: str,
    label: str,
    worker_id: int,
    step_limit: int,
    start: Any,
    queue: Any,
) -> None:
    completed = 0
    with PostgresStore(dsn, store_id=store_id) as store:
        run = Runtime(store).run(label, budget=Budget(steps=step_limit))
        start.wait()
        for index in range(step_limit + 1):
            try:
                run.model_call(
                    {"model": "mock", "worker": worker_id, "index": index},
                    attempt=worker_id * 100 + index,
                    fn=lambda _payload: {},
                )
                completed += 1
            except BudgetExceeded:
                break
    queue.put(completed)


def _run_eight_writer_window_round(dsn: str) -> int:
    store_id = f"window-eight-{uuid4().hex}"
    label = f"window-eight-{uuid4().hex}"
    barrier = Barrier(8)
    lock = Lock()
    executed: list[tuple[int, int]] = []

    def worker(worker_id: int) -> None:
        with PostgresStore(dsn, store_id=store_id) as store:
            run = Runtime(
                store,
                meters=[WindowMeter("requests", 16, 60)],
            ).run(label)
            barrier.wait()
            for index in range(4):
                try:
                    run.model_call(
                        {"model": "mock", "worker": worker_id, "index": index},
                        attempt=worker_id * 100 + index,
                        fn=lambda _payload, value=(worker_id, index): _thread_result(
                            executed, lock, value
                        ),
                    )
                except BudgetExceeded:
                    break

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(8)))
    return len(executed)


def _thread_result(
    target: list[tuple[int, int]],
    lock: Lock,
    value: tuple[int, int],
) -> dict[str, bool]:
    with lock:
        target.append(value)
    return {"ok": True}


@contextmanager
def _isolated_schema_dsn(dsn: str) -> Iterator[str]:
    psycopg = importlib.import_module("psycopg")
    make_conninfo = importlib.import_module("psycopg.conninfo").make_conninfo
    schema = f"pollard_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    isolated_dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
    try:
        yield isolated_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _schema_version(dsn: str) -> int:
    psycopg = importlib.import_module("psycopg")
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT version FROM pollard_schema WHERE singleton = 1"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _create_legacy_schema(dsn: str) -> None:
    psycopg = importlib.import_module("psycopg")
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in (
            """
            CREATE TABLE pollard_nodes (
              store_id TEXT NOT NULL, id TEXT NOT NULL, parent TEXT,
              kind TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL,
              result TEXT, result_digest TEXT, meta TEXT NOT NULL,
              PRIMARY KEY (store_id, id)
            )
            """,
            """
            CREATE TABLE pollard_blobs (
              store_id TEXT NOT NULL, digest TEXT NOT NULL, value TEXT NOT NULL,
              PRIMARY KEY (store_id, digest)
            )
            """,
            """
            CREATE TABLE pollard_blob_literals (
              store_id TEXT NOT NULL, node_id TEXT NOT NULL, path TEXT NOT NULL,
              PRIMARY KEY (store_id, node_id, path)
            )
            """,
            """
            CREATE TABLE pollard_budget_state (
              store_id TEXT NOT NULL, scope_id TEXT NOT NULL, meter TEXT NOT NULL,
              settled NUMERIC NOT NULL, PRIMARY KEY (store_id, scope_id, meter)
            )
            """,
            """
            CREATE TABLE pollard_reservations (
              store_id TEXT NOT NULL, reservation_id TEXT NOT NULL,
              kind TEXT NOT NULL, scope_id TEXT NOT NULL, meter TEXT NOT NULL,
              amount NUMERIC NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
              window_seconds DOUBLE PRECISION,
              PRIMARY KEY (store_id, reservation_id, kind, scope_id, meter)
            )
            """,
            """
            CREATE TABLE pollard_window_scopes (
              store_id TEXT NOT NULL, ledger_key TEXT NOT NULL,
              PRIMARY KEY (store_id, ledger_key)
            )
            """,
            """
            CREATE TABLE pollard_window_events (
              event_id BIGSERIAL PRIMARY KEY, store_id TEXT NOT NULL,
              scope_id TEXT NOT NULL, meter TEXT NOT NULL,
              amount NUMERIC NOT NULL, settled_at DOUBLE PRECISION NOT NULL
            )
            """,
        ):
            conn.execute(statement)


def _wait_for_postgres_lock(conn: Any, backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
            (backend_pid,),
        ).fetchone()
        if row is not None and str(row[0]) == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError(f"PostgreSQL backend {backend_pid} did not wait for a lock")


def _step_reservation() -> BudgetReservation:
    return BudgetReservation(
        scope_id=f"scope-{uuid4().hex}",
        limits={"steps": Decimal("2")},
        baseline={},
        estimates={"steps": Decimal("1")},
    )
