import importlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
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
from pollard.cli import main
from pollard.tree import Node, NodeKind


def test_postgres_store_is_lazy_optional_import() -> None:
    module = importlib.import_module("pollard.stores.postgres")
    assert module.PostgresStore is PostgresStore


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
