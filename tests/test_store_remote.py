from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from pollard import (
    Budget,
    MongoStore,
    Neo4jStore,
    RedisStore,
    ReplayMode,
    Runtime,
    SQLiteSealSink,
    seal,
    verify,
)
from pollard.arbiter import BudgetReservation, TransactionalArbiter, WindowReservation
from pollard.errors import IntegrityError
from pollard.tree import Node, NodeKind

_REMOTE_BACKENDS: list[str] = []
if os.environ.get("POLLARD_TEST_REDIS_URL"):
    _REMOTE_BACKENDS.append("redis")
if os.environ.get("POLLARD_TEST_MONGODB_URI"):
    _REMOTE_BACKENDS.append("mongodb")
if os.environ.get("POLLARD_TEST_NEO4J_URI"):
    _REMOTE_BACKENDS.append("neo4j")


def _open(name: str, store_id: str) -> Any:
    if name == "redis":
        return RedisStore(os.environ["POLLARD_TEST_REDIS_URL"], store_id=store_id)
    if name == "mongodb":
        return MongoStore(
            os.environ["POLLARD_TEST_MONGODB_URI"],
            database=os.environ.get("POLLARD_TEST_MONGODB_DATABASE", "pollard_test"),
            store_id=store_id,
        )
    if name == "neo4j":
        return Neo4jStore(
            os.environ["POLLARD_TEST_NEO4J_URI"],
            (
                os.environ.get("POLLARD_TEST_NEO4J_USER", "neo4j"),
                os.environ["POLLARD_TEST_NEO4J_PASSWORD"],
            ),
            database=os.environ.get("POLLARD_TEST_NEO4J_DATABASE", "neo4j"),
            store_id=store_id,
        )
    raise AssertionError(name)


@contextmanager
def _pair(name: str) -> Iterator[tuple[Any, Any, str]]:
    store_id = f"remote-{uuid4().hex}"
    first = _open(name, store_id)
    second = _open(name, store_id)
    try:
        yield first, second, store_id
    finally:
        first.close()
        second.close()


def _budget(limit: int = 4) -> BudgetReservation:
    return BudgetReservation(
        scope_id="shared-budget",
        limits={"steps": Decimal(limit)},
        baseline={},
        estimates={"steps": Decimal("1")},
    )


@pytest.mark.parametrize("backend", _REMOTE_BACKENDS)
def test_remote_store_reopen_and_exact_concurrent_budget(backend: str) -> None:
    with _pair(backend) as (first, second, store_id):
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": f"persist-{backend}"},
        )
        first.put(root)
        assert second.get(root.id) == root

        stores = [first, second]

        def reserve(index: int) -> bool:
            return bool(
                stores[index % 2]._pollard_reserve(
                    f"reservation-{index}", [_budget()], [], 60
                ).ok
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(reserve, range(8)))
        assert results.count(True) == 4
        assert results.count(False) == 4

        first.reconnect()
        assert first.get(root.id) == root
        with _open(backend, store_id) as reopened:
            assert reopened.get(root.id) == root
        assert isinstance(first, TransactionalArbiter)


@pytest.mark.parametrize("backend", _REMOTE_BACKENDS)
def test_remote_window_contention_and_retry_tombstones(backend: str) -> None:
    request = WindowReservation(
        ledger_key="shared-window",
        meter="requests",
        limit=Decimal("3"),
        amount=Decimal("1"),
        window_seconds=60,
    )
    with _pair(backend) as (first, second, _store_id):
        stores = [first, second]

        def reserve(index: int) -> tuple[str, bool]:
            reservation_id = f"window-{index}"
            result = stores[index % 2]._pollard_reserve(
                reservation_id, [], [request], 60
            )
            return reservation_id, result.ok

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(reserve, range(8)))
        accepted = [reservation_id for reservation_id, ok in outcomes if ok]
        assert len(accepted) == 3
        first._pollard_settle(accepted[0], {"requests": Decimal("1")})
        first._pollard_settle(accepted[0], {"requests": Decimal("1")})
        with pytest.raises(IntegrityError, match="different charges"):
            first._pollard_settle(accepted[0], {"requests": Decimal("2")})


@pytest.mark.parametrize("backend", _REMOTE_BACKENDS)
def test_remote_replay_verify_seal_and_external_custody(
    backend: str, tmp_path: Path
) -> None:
    store_id = f"evidence-{uuid4().hex}"
    with _open(backend, store_id) as store:
        run = Runtime(store).run("evidence", budget=Budget(steps=2))
        expected = run.model_call(
            {"model": "offline"}, fn=lambda _payload: {"text": "recorded"}
        )
        report = seal(store, run.root_id)
        assert verify(store, run.root_id).ok
        custody = SQLiteSealSink(tmp_path / f"{backend}-custody.db")
        publication = custody.publish(
            report, store_id=store_id, signer_identity="test:independent-custody"
        )
        assert publication.digest == report.digest

    with _open(backend, store_id) as reopened:
        replay = Runtime(reopened, mode=ReplayMode.REPLAY).run(
            "evidence", budget=Budget(steps=2)
        )
        actual = replay.model_call(
            {"model": "offline"},
            fn=lambda _payload: pytest.fail("strict replay executed the callable"),
        )
        assert actual == expected


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_REDIS_URL"), reason="Redis is not configured"
)
def test_redis_requires_noeviction_and_refuses_future_schema() -> None:
    store = RedisStore(
        os.environ["POLLARD_TEST_REDIS_URL"], store_id=f"schema-{uuid4().hex}"
    )
    try:
        assert store._client.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"
        store._client.hset(store._bucket_key("schema"), "version", "999")
        with pytest.raises(IntegrityError, match="schema version: 999"):
            store.reconnect()
    finally:
        store.close()


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_REDIS_URL"), reason="Redis is not configured"
)
def test_redis_caller_owned_client_factory_reconnects() -> None:
    from redis import Redis

    url = os.environ["POLLARD_TEST_REDIS_URL"]
    clients: list[Redis] = []

    def factory() -> Redis:
        client = Redis.from_url(url, decode_responses=True)
        clients.append(client)
        return client

    store_id = f"factory-{uuid4().hex}"
    with RedisStore(client_factory=factory, store_id=store_id) as store:
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "redis-client-factory"},
        )
        store.put(root)
        store.reconnect()
        assert store.get(root.id) == root

    assert len(clients) == 2


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_MONGODB_URI"), reason="MongoDB is not configured"
)
def test_mongodb_refuses_future_schema() -> None:
    store = _open("mongodb", f"schema-{uuid4().hex}")
    try:
        store._records.update_one(
            {"store_id": store.store_id, "bucket": "schema", "key": "version"},
            {"$set": {"value": "999"}},
        )
        with pytest.raises(IntegrityError, match="schema version: 999"):
            store.reconnect()
    finally:
        store.close()


@pytest.mark.skipif(
    not os.environ.get("POLLARD_TEST_NEO4J_URI"), reason="Neo4j is not configured"
)
def test_neo4j_refuses_future_schema() -> None:
    store = _open("neo4j", f"schema-{uuid4().hex}")
    try:
        with store._driver.session(database=store.database) as session:
            session.run(
                """
                MATCH (record:_PollardKV)
                WHERE record.store_id = $store_id
                  AND record.bucket = 'schema' AND record.item_key = 'version'
                SET record.value = '999'
                """,
                store_id=store.store_id,
            ).consume()
        with pytest.raises(IntegrityError, match="schema version: 999"):
            store.reconnect()
    finally:
        store.close()
