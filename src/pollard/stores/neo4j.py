"""Neo4j-backed transactional store.

The adapter uses Neo4j as a small transactional key/value substrate.  Every
write for one logical Pollard store takes a write lock on a coordinator node;
the shared :class:`~pollard.stores._transactional.TransactionalKVStore` then
implements node persistence and exact reservation accounting on top.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from importlib import import_module
from typing import Any, TypeVar

from pollard._canon import canonical_bytes
from pollard.errors import IntegrityError

from ._transactional import KVTransaction, TransactionalKVStore

T = TypeVar("T")

NEO4J_SCHEMA_VERSION = 1

_KV_LABEL = "_PollardKV"
_COORDINATOR_LABEL = "_PollardCoordinator"
_CONSTRAINTS = (
    """
    CREATE CONSTRAINT pollard_neo4j_kv_record_key IF NOT EXISTS
    FOR (record:_PollardKV) REQUIRE record.record_key IS UNIQUE
    """,
    """
    CREATE CONSTRAINT pollard_neo4j_coordinator_key IF NOT EXISTS
    FOR (coordinator:_PollardCoordinator)
    REQUIRE coordinator.coordinator_key IS UNIQUE
    """,
)


class Neo4jStore(TransactionalKVStore):
    """A logical Pollard store in Neo4j, isolated by ``store_id``.

    ``Driver`` objects are shared across calls, while each operation gets a
    short-lived write-routed session.  Write routing is intentional even for
    reads: it prevents a different process from observing stale follower state
    immediately after a commit in a Neo4j cluster.
    """

    backend_name = "Neo4j"

    def __init__(
        self,
        uri: str,
        auth: object,
        *,
        database: str = "neo4j",
        store_id: str = "default",
        **driver_config: Any,
    ) -> None:
        if not isinstance(uri, str) or not uri:
            raise ValueError("uri must be a non-empty string")
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a non-empty string")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        try:
            neo4j = import_module("neo4j")
        except ImportError as exc:
            raise ImportError(
                "Neo4jStore requires the 'neo4j' extra: pip install 'pollard[neo4j]'"
            ) from exc

        self.uri = uri
        self.database = database
        self.store_id = store_id
        self._auth = auth
        self._driver_config = dict(driver_config)
        self._neo4j = neo4j
        self._driver: Any = self._connect()
        try:
            self._ensure_constraints(self._driver)
            self._initialize_transactional_store()
        except BaseException:
            self._driver.close()
            raise

    def __enter__(self) -> Neo4jStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the driver's connection pool."""

        self._driver.close()

    def reconnect(self) -> None:
        """Replace the driver and refuse a missing or incompatible schema."""

        driver = self._connect()
        try:
            self._ensure_constraints(driver)
            version = self._execute(
                driver,
                lambda tx: tx.get("schema", "version"),
                lock=False,
            )
            if version is None:
                raise IntegrityError("missing Neo4j Pollard schema")
            if version != str(NEO4J_SCHEMA_VERSION):
                raise IntegrityError(f"unsupported Neo4j schema version: {version}")
        except BaseException:
            driver.close()
            raise

        previous = self._driver
        self._driver = driver
        previous.close()

    def _connect(self) -> Any:
        driver = self._neo4j.GraphDatabase.driver(
            self.uri,
            auth=self._auth,
            **self._driver_config,
        )
        try:
            driver.verify_connectivity()
        except BaseException:
            driver.close()
            raise
        return driver

    def _read(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._execute(self._driver, callback, lock=False)

    def _write(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._execute(self._driver, callback, lock=True)

    def _execute(
        self,
        driver: Any,
        callback: Callable[[KVTransaction], T],
        *,
        lock: bool,
    ) -> T:
        session_config: dict[str, object] = {
            "database": self.database,
            "default_access_mode": self._neo4j.WRITE_ACCESS,
        }
        bookmark_manager = getattr(driver, "execute_query_bookmark_manager", None)
        if bookmark_manager is not None:
            session_config["bookmark_manager"] = bookmark_manager

        def work(transaction: Any) -> T:
            kv = _Neo4jKVTransaction(transaction, self.store_id)
            if lock:
                kv.lock()
            return callback(kv)

        with driver.session(**session_config) as session:
            result: T = session.execute_write(work)
            return result

    def _ensure_constraints(self, driver: Any) -> None:
        session_config: dict[str, object] = {
            "database": self.database,
            "default_access_mode": self._neo4j.WRITE_ACCESS,
        }
        bookmark_manager = getattr(driver, "execute_query_bookmark_manager", None)
        if bookmark_manager is not None:
            session_config["bookmark_manager"] = bookmark_manager
        with driver.session(**session_config) as session:
            for statement in _CONSTRAINTS:
                session.execute_write(
                    lambda transaction, query=statement: transaction.run(query).consume()
                )

    def _is_connection_error(self, exc: BaseException) -> bool:
        exceptions = getattr(self._neo4j, "exceptions", None)
        if exceptions is None:
            return False
        connection_errors = tuple(
            candidate
            for name in (
                "ServiceUnavailable",
                "SessionExpired",
                "ConnectionAcquisitionTimeoutError",
            )
            if isinstance((candidate := getattr(exceptions, name, None)), type)
        )
        return bool(connection_errors) and isinstance(exc, connection_errors)


class _Neo4jKVTransaction:
    """``KVTransaction`` implementation over one managed Neo4j transaction."""

    def __init__(self, transaction: Any, store_id: str) -> None:
        self._transaction = transaction
        self._store_id = store_id
        self._locked = False

    def lock(self) -> None:
        coordinator_key = _coordinator_key(self._store_id)
        record = self._transaction.run(
            f"""
            MERGE (coordinator:{_COORDINATOR_LABEL}
                   {{coordinator_key: $coordinator_key}})
            ON CREATE SET coordinator.store_id = $store_id,
                          coordinator.revision = 0
            SET coordinator.revision = coordinator.revision + 1
            RETURN coordinator.coordinator_key AS coordinator_key,
                   coordinator.store_id AS store_id,
                   coordinator.revision AS revision
            """,
            coordinator_key=coordinator_key,
            store_id=self._store_id,
        ).single()
        if record is None:
            raise IntegrityError("Neo4j coordinator disappeared while locking")
        revision = record["revision"]
        if (
            record["coordinator_key"] != coordinator_key
            or record["store_id"] != self._store_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            raise IntegrityError("Neo4j coordinator key collision or corruption")
        self._locked = True

    def get(self, bucket: str, key: str) -> str | None:
        record_key = _record_key(self._store_id, bucket, key)
        record = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL} {{record_key: $record_key}})
            RETURN record.record_key AS record_key,
                   record.store_id AS store_id,
                   record.bucket AS bucket,
                   record.item_key AS item_key,
                   record.value AS value
            """,
            record_key=record_key,
        ).single()
        if record is None:
            return None
        return self._validated_value(record, bucket, key, record_key)

    def items(self, bucket: str) -> list[tuple[str, str]]:
        records = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL}
                   {{store_id: $store_id, bucket: $bucket}})
            RETURN record.record_key AS record_key,
                   record.store_id AS store_id,
                   record.bucket AS bucket,
                   record.item_key AS item_key,
                   record.value AS value
            ORDER BY record.item_key ASC
            """,
            store_id=self._store_id,
            bucket=bucket,
        )
        items: list[tuple[str, str]] = []
        for record in records:
            key = record["item_key"]
            if not isinstance(key, str):
                raise IntegrityError("Neo4j record key is not a string")
            record_key = _record_key(self._store_id, bucket, key)
            value = self._validated_value(record, bucket, key, record_key)
            items.append((key, value))
        return items

    def put(self, bucket: str, key: str, value: str) -> None:
        self._require_lock()
        record_key = _record_key(self._store_id, bucket, key)
        record = self._transaction.run(
            f"""
            MERGE (record:{_KV_LABEL} {{record_key: $record_key}})
            ON CREATE SET record.store_id = $store_id,
                          record.bucket = $bucket,
                          record.item_key = $item_key,
                          record.value = $value
            RETURN record.record_key AS record_key,
                   record.store_id AS store_id,
                   record.bucket AS bucket,
                   record.item_key AS item_key,
                   record.value AS value
            """,
            record_key=record_key,
            store_id=self._store_id,
            bucket=bucket,
            item_key=key,
            value=value,
        ).single()
        if record is None:
            raise IntegrityError("Neo4j record disappeared during put")
        self._validated_value(record, bucket, key, record_key)
        updated = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL} {{record_key: $record_key}})
            WHERE record.store_id = $store_id
              AND record.bucket = $bucket
              AND record.item_key = $item_key
            SET record.value = $value
            RETURN record.record_key AS record_key
            """,
            record_key=record_key,
            store_id=self._store_id,
            bucket=bucket,
            item_key=key,
            value=value,
        ).single()
        if updated is None or updated["record_key"] != record_key:
            raise IntegrityError("Neo4j record changed during put")

    def delete(self, bucket: str, key: str) -> None:
        self._require_lock()
        if self.get(bucket, key) is None:
            return
        record_key = _record_key(self._store_id, bucket, key)
        deleted = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL} {{record_key: $record_key}})
            WHERE record.store_id = $store_id
              AND record.bucket = $bucket
              AND record.item_key = $item_key
            DELETE record
            RETURN count(*) AS deleted
            """,
            record_key=record_key,
            store_id=self._store_id,
            bucket=bucket,
            item_key=key,
        ).single()
        if deleted is None or deleted["deleted"] != 1:
            raise IntegrityError("Neo4j record changed during delete")

    def now(self) -> float:
        self._require_lock()
        record = self._transaction.run(
            "RETURN datetime.realtime().epochMillis AS epoch_millis"
        ).single()
        if record is None:
            raise IntegrityError("Neo4j server clock returned no value")
        epoch_millis = record["epoch_millis"]
        if isinstance(epoch_millis, bool) or not isinstance(epoch_millis, int):
            raise IntegrityError("Neo4j server clock returned an invalid value")
        return float(epoch_millis) / 1000.0

    def _validated_value(
        self,
        record: Any,
        bucket: str,
        key: str,
        record_key: str,
    ) -> str:
        value = record["value"]
        if (
            record["record_key"] != record_key
            or record["store_id"] != self._store_id
            or record["bucket"] != bucket
            or record["item_key"] != key
            or not isinstance(value, str)
        ):
            raise IntegrityError("Neo4j record key collision or corruption")
        return value

    def _require_lock(self) -> None:
        if not self._locked:
            raise RuntimeError("Neo4j write transaction does not hold its coordinator lock")


def _record_key(store_id: str, bucket: str, key: str) -> str:
    encoded = canonical_bytes(["neo4j-record", store_id, bucket, key])
    return hashlib.sha256(encoded).hexdigest()


def _coordinator_key(store_id: str) -> str:
    encoded = canonical_bytes(["neo4j-coordinator", store_id])
    return hashlib.sha256(encoded).hexdigest()
