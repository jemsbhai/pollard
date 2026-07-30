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
_EXPECTED_CONSTRAINTS = {
    "pollard_neo4j_kv_record_key": (_KV_LABEL, "record_key"),
    "pollard_neo4j_coordinator_key": (
        _COORDINATOR_LABEL,
        "coordinator_key",
    ),
}
_SCHEMA_VERSION = "1"
_SCHEMA_BUCKET = "schema"
_SCHEMA_KEY = "version"


class Neo4jStore(TransactionalKVStore):
    """A logical Pollard store in Neo4j, isolated by ``store_id``.

    ``Driver`` objects are shared across calls, while each operation gets a
    short-lived write-routed session.  Write routing is intentional even for
    reads: it prevents a different process from observing stale follower state
    immediately after a commit in a Neo4j cluster. ``create=False`` validates
    an existing logical store and its constraints/backing indexes without
    creating constraints, indexes, coordinator nodes, or schema records.
    """

    backend_name = "Neo4j"

    def __init__(
        self,
        uri: str,
        auth: object,
        *,
        database: str = "neo4j",
        store_id: str = "default",
        create: bool = True,
        **driver_config: Any,
    ) -> None:
        if not isinstance(uri, str) or not uri:
            raise ValueError("uri must be a non-empty string")
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a non-empty string")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        if not isinstance(create, bool):
            raise TypeError("create must be a boolean")
        try:
            neo4j = import_module("neo4j")
        except ImportError as exc:
            raise ImportError(
                "Neo4jStore requires the 'neo4j' extra: pip install 'pollard[neo4j]'"
            ) from exc

        self.uri = uri
        self.database = database
        self.store_id = store_id
        self.create = create
        self._auth = auth
        self._driver_config = dict(driver_config)
        self._neo4j = neo4j
        self._driver: Any = self._connect()
        try:
            if create:
                self._initialize_or_require_store()
            else:
                self._require_existing_store()
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
        previous = self._driver
        self._driver = driver
        try:
            self._require_existing_store()
        except BaseException:
            self._driver = previous
            driver.close()
            raise
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

    def _initialize_or_require_store(self) -> None:
        state = self._namespace_state_read(self._driver)
        if state == "existing":
            self._require_constraints(self._driver)
            return
        self._ensure_constraints(self._driver)
        self._initialize_fresh_namespace()
        self._require_constraints(self._driver)

    def _require_existing_store(self) -> None:
        if self._namespace_state_read(self._driver) == "fresh":
            raise IntegrityError("unsupported Neo4j schema version: missing")
        self._require_constraints(self._driver)

    def _namespace_state_read(self, driver: Any) -> str:
        return self._execute(
            driver,
            self._namespace_state,
            lock=False,
        )

    def _namespace_state(self, transaction: KVTransaction) -> str:
        if not isinstance(transaction, _Neo4jKVTransaction):
            raise TypeError("Neo4j namespace validation requires a Neo4j transaction")
        version = transaction.get(_SCHEMA_BUCKET, _SCHEMA_KEY)
        coordinator = transaction.coordinator()
        if version is None:
            if not transaction.has_records() and coordinator is None:
                return "fresh"
            raise IntegrityError("Neo4j store initialization state is partial")
        if coordinator is None:
            raise IntegrityError("Neo4j store initialization state is partial")
        if version != _SCHEMA_VERSION:
            shown = ascii(version)[1:-1]
            raise IntegrityError(f"unsupported Neo4j schema version: {shown}")
        transaction.validate_coordinator(coordinator)
        return "existing"

    def _initialize_fresh_namespace(self) -> None:
        def initialize(transaction: KVTransaction) -> None:
            if not isinstance(transaction, _Neo4jKVTransaction):
                raise TypeError("Neo4j initialization requires a Neo4j transaction")
            if self._namespace_state(transaction) == "existing":
                return
            transaction.initialize_coordinator()
            transaction.put(_SCHEMA_BUCKET, _SCHEMA_KEY, _SCHEMA_VERSION)

        self._execute(self._driver, initialize, lock=False)

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
        state = self._constraint_state(
            self._constraint_rows(driver),
            self._index_rows(driver),
        )
        if state == "existing":
            return
        if state == "partial":
            raise IntegrityError(
                "Neo4j Pollard constraints are missing or incompatible"
            )
        session_config: dict[str, object] = {
            "database": self.database,
            "default_access_mode": self._neo4j.WRITE_ACCESS,
        }
        bookmark_manager = getattr(driver, "execute_query_bookmark_manager", None)
        if bookmark_manager is not None:
            session_config["bookmark_manager"] = bookmark_manager

        def create_constraints(transaction: Any) -> None:
            for statement in _CONSTRAINTS:
                transaction.run(statement).consume()

        with driver.session(**session_config) as session:
            session.execute_write(create_constraints)
        self._require_constraints(driver)

    def _require_constraints(self, driver: Any) -> None:
        if (
            self._constraint_state(
                self._constraint_rows(driver),
                self._index_rows(driver),
            )
            != "existing"
        ):
            raise IntegrityError(
                "Neo4j Pollard constraints are missing or incompatible"
            )

    def _constraint_rows(self, driver: Any) -> list[dict[str, object]]:
        session_config: dict[str, object] = {
            "database": self.database,
            "default_access_mode": self._neo4j.WRITE_ACCESS,
        }
        bookmark_manager = getattr(driver, "execute_query_bookmark_manager", None)
        if bookmark_manager is not None:
            session_config["bookmark_manager"] = bookmark_manager

        def read_constraints(transaction: Any) -> list[dict[str, object]]:
            rows = transaction.run(
                """
                SHOW CONSTRAINTS
                YIELD name, type, entityType, labelsOrTypes, properties,
                      ownedIndex
                RETURN name, type, entityType, labelsOrTypes, properties,
                       ownedIndex
                """
            ).data()
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise IntegrityError("Neo4j constraint metadata is invalid")
            return rows

        with driver.session(**session_config) as session:
            rows: list[dict[str, object]] = session.execute_write(
                read_constraints
            )
        return rows

    def _index_rows(self, driver: Any) -> list[dict[str, object]]:
        session_config: dict[str, object] = {
            "database": self.database,
            "default_access_mode": self._neo4j.WRITE_ACCESS,
        }
        bookmark_manager = getattr(driver, "execute_query_bookmark_manager", None)
        if bookmark_manager is not None:
            session_config["bookmark_manager"] = bookmark_manager

        def read_indexes(transaction: Any) -> list[dict[str, object]]:
            rows = transaction.run(
                """
                SHOW INDEXES
                YIELD name, state, type, entityType, labelsOrTypes, properties,
                      owningConstraint
                RETURN name, state, type, entityType, labelsOrTypes, properties,
                       owningConstraint
                """
            ).data()
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise IntegrityError("Neo4j index metadata is invalid")
            return rows

        with driver.session(**session_config) as session:
            rows: list[dict[str, object]] = session.execute_write(read_indexes)
        return rows

    def _constraint_state(
        self,
        rows: list[dict[str, object]],
        index_rows: list[dict[str, object]],
    ) -> str:
        by_name = {
            row.get("name"): row
            for row in rows
            if isinstance(row.get("name"), str)
        }
        indexes_by_name = {
            row.get("name"): row
            for row in index_rows
            if isinstance(row.get("name"), str)
        }
        expected_names = _EXPECTED_CONSTRAINTS.keys()
        present_names = expected_names & by_name.keys()
        present_index_names = expected_names & indexes_by_name.keys()
        if not present_names and not present_index_names:
            expected_schemas = set(_EXPECTED_CONSTRAINTS.values())
            for constraint_row in rows:
                signature = self._constraint_signature(constraint_row)
                if signature in expected_schemas:
                    return "partial"
            for index_row in index_rows:
                signature = self._index_signature(index_row)
                if signature in expected_schemas:
                    return "partial"
            return "fresh"
        for name, (label, property_name) in _EXPECTED_CONSTRAINTS.items():
            constraint = by_name.get(name)
            index = indexes_by_name.get(name)
            if (
                constraint is None
                or constraint.get("type")
                not in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}
                or constraint.get("entityType") != "NODE"
                or constraint.get("labelsOrTypes") != [label]
                or constraint.get("properties") != [property_name]
                or constraint.get("ownedIndex") != name
                or index is None
                or index.get("state") != "ONLINE"
                or index.get("type") != "RANGE"
                or index.get("entityType") != "NODE"
                or index.get("labelsOrTypes") != [label]
                or index.get("properties") != [property_name]
                or index.get("owningConstraint") != name
            ):
                return "partial"
        return "existing"

    @staticmethod
    def _constraint_signature(
        row: dict[str, object],
    ) -> tuple[str, str] | None:
        labels = row.get("labelsOrTypes")
        properties = row.get("properties")
        if (
            row.get("type") not in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}
            or row.get("entityType") != "NODE"
            or not isinstance(labels, list)
            or len(labels) != 1
            or not isinstance(labels[0], str)
            or not isinstance(properties, list)
            or len(properties) != 1
            or not isinstance(properties[0], str)
        ):
            return None
        return labels[0], properties[0]

    @staticmethod
    def _index_signature(
        row: dict[str, object],
    ) -> tuple[str, str] | None:
        labels = row.get("labelsOrTypes")
        properties = row.get("properties")
        if (
            row.get("type") != "RANGE"
            or row.get("entityType") != "NODE"
            or not isinstance(labels, list)
            or len(labels) != 1
            or not isinstance(labels[0], str)
            or not isinstance(properties, list)
            or len(properties) != 1
            or not isinstance(properties[0], str)
        ):
            return None
        return labels[0], properties[0]

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
        existing = self.coordinator()
        if existing is None:
            raise IntegrityError("Neo4j coordinator is missing or corrupt")
        self.validate_coordinator(existing)
        record = self._transaction.run(
            f"""
            MATCH (coordinator:{_COORDINATOR_LABEL}
                   {{coordinator_key: $coordinator_key,
                     store_id: $store_id}})
            SET coordinator.revision = coordinator.revision + 1
            RETURN properties(coordinator) AS properties
            """,
            coordinator_key=coordinator_key,
            store_id=self._store_id,
        ).single()
        if record is None:
            raise IntegrityError("Neo4j coordinator disappeared while locking")
        properties = record["properties"]
        self.validate_coordinator(properties)
        self._locked = True

    def has_records(self) -> bool:
        record = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL} {{store_id: $store_id}})
            RETURN record.record_key AS record_key
            LIMIT 1
            """,
            store_id=self._store_id,
        ).single()
        return record is not None

    def coordinator(self) -> dict[str, object] | None:
        coordinator_key = _coordinator_key(self._store_id)
        record = self._transaction.run(
            f"""
            MATCH (coordinator:{_COORDINATOR_LABEL}
                   {{coordinator_key: $coordinator_key}})
            RETURN properties(coordinator) AS properties
            """,
            coordinator_key=coordinator_key,
        ).single()
        if record is None:
            return None
        properties = record["properties"]
        if not isinstance(properties, dict):
            raise IntegrityError("Neo4j coordinator properties are invalid")
        return properties

    def initialize_coordinator(self) -> None:
        coordinator_key = _coordinator_key(self._store_id)
        record = self._transaction.run(
            f"""
            MERGE (coordinator:{_COORDINATOR_LABEL}
                   {{coordinator_key: $coordinator_key}})
            ON CREATE SET coordinator.store_id = $store_id,
                          coordinator.revision = 1
            RETURN properties(coordinator) AS properties
            """,
            coordinator_key=coordinator_key,
            store_id=self._store_id,
        ).single()
        if record is None:
            raise IntegrityError("Neo4j coordinator disappeared during initialization")
        properties = record["properties"]
        self.validate_coordinator(properties)
        self._locked = True

    def validate_coordinator(self, properties: object) -> None:
        coordinator_key = _coordinator_key(self._store_id)
        if not isinstance(properties, dict):
            raise IntegrityError("Neo4j coordinator properties are invalid")
        revision = properties.get("revision")
        if (
            properties.get("coordinator_key") != coordinator_key
            or properties.get("store_id") != self._store_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise IntegrityError("Neo4j coordinator key collision or corruption")

    def get(self, bucket: str, key: str) -> str | None:
        record_key = _record_key(self._store_id, bucket, key)
        record = self._transaction.run(
            f"""
            MATCH (record:{_KV_LABEL} {{record_key: $record_key}})
            RETURN properties(record) AS properties
            """,
            record_key=record_key,
        ).single()
        if record is None:
            return None
        properties = record["properties"]
        if not isinstance(properties, dict):
            raise IntegrityError("Neo4j record properties are invalid")
        return self._validated_value(properties, bucket, key, record_key)

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
