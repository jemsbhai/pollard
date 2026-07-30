"""MongoDB-backed store for shared, multi-writer runs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from importlib import import_module
from typing import Any, TypeVar, cast

from pollard._canon import canonical_bytes
from pollard.errors import IntegrityError

from ._transactional import KVTransaction, TransactionalKVStore

T = TypeVar("T")
_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MongoConnection = tuple[Any, Any, Any, Any]
_RECORD_INDEX = [("store_id", 1), ("bucket", 1), ("key", 1)]
_RECORD_INDEX_NAME = "pollard_store_bucket_key_unique"
_SCHEMA_VERSION = "1"
_SCHEMA_BUCKET = "schema"
_SCHEMA_KEY = "version"


class MongoStore(TransactionalKVStore):
    """A transactional logical Pollard store in MongoDB.

    MongoDB transactions require a replica set or sharded deployment. A
    standalone server is refused instead of silently weakening accounting.
    ``create=False`` validates an existing logical store without creating its
    collections, index, coordinator, or schema.
    """

    backend_name = "MongoDB"

    def __init__(
        self,
        uri: str,
        *,
        database: str = "pollard",
        store_id: str = "default",
        collection_prefix: str = "pollard",
        create: bool = True,
        **client_options: object,
    ) -> None:
        if not isinstance(uri, str) or not uri:
            raise ValueError("uri must be a non-empty string")
        if not isinstance(database, str) or not database or "\x00" in database:
            raise ValueError("database must be a non-empty MongoDB database name")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        if not isinstance(collection_prefix, str) or not _PREFIX.fullmatch(
            collection_prefix
        ):
            raise ValueError(
                "collection_prefix must start with a letter and contain only "
                "letters, digits, and underscores"
            )
        if not isinstance(create, bool):
            raise TypeError("create must be a boolean")
        try:
            pymongo = import_module("pymongo")
        except ImportError as exc:
            raise ImportError(
                "MongoStore requires the 'mongodb' extra: "
                "pip install 'pollard[mongodb]'"
            ) from exc
        self.uri = uri
        self.database_name = database
        self.store_id = store_id
        self.collection_prefix = collection_prefix
        self.create = create
        self._client_options = dict(client_options)
        self._pymongo = pymongo
        self._client: Any = None
        self._database: Any = None
        self._records: Any = None
        self._coordinators: Any = None
        self._set_connection(self._connect())
        try:
            if create:
                self._initialize_or_require_store()
            else:
                self._require_existing_store()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> MongoStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def reconnect(self) -> None:
        """Atomically replace the client after topology and schema validation."""

        replacement = self._connect()
        previous = self._connection()
        self._set_connection(replacement)
        try:
            self._require_existing_store()
        except BaseException:
            self._set_connection(previous)
            replacement[0].close()
            raise
        previous[0].close()

    def _connect(self) -> _MongoConnection:
        client = self._pymongo.MongoClient(self.uri, **self._client_options)
        try:
            topology = client.admin.command("hello")
            if topology.get("setName") is None and topology.get("msg") != "isdbgrid":
                raise ValueError(
                    "MongoStore requires a replica set or sharded MongoDB deployment"
                )
            database = client[self.database_name]
            records = database[f"{self.collection_prefix}_records"]
            coordinators = database[
                f"{self.collection_prefix}_coordinators"
            ]
            return client, database, records, coordinators
        except BaseException:
            client.close()
            raise

    def _initialize_or_require_store(self) -> None:
        state = self._namespace_state_read()
        if state == "existing":
            self._require_record_index()
            return
        self._ensure_record_index_for_fresh_namespace()
        self._initialize_fresh_namespace()
        self._require_record_index()

    def _require_existing_store(self) -> None:
        if self._namespace_state_read() == "fresh":
            raise IntegrityError(
                "unsupported MongoDB schema version: missing"
            )
        self._require_record_index()

    def _namespace_state_read(self) -> str:
        read_concern, write_concern, primary = self._transaction_options()
        with (
            self._client.start_session() as session,
            session.start_transaction(
                read_concern=read_concern,
                write_concern=write_concern,
                read_preference=primary,
            ),
        ):
            return self._namespace_state(session)

    def _namespace_state(self, session: Any) -> str:
        schema_id = _record_id(self.store_id, _SCHEMA_BUCKET, _SCHEMA_KEY)
        schema = self._records.find_one({"_id": schema_id}, session=session)
        coordinator = self._coordinators.find_one(
            {"_id": self.store_id},
            session=session,
        )
        if schema is None:
            any_record = self._records.find_one(
                {"store_id": self.store_id},
                session=session,
            )
            if any_record is None and coordinator is None:
                return "fresh"
            raise IntegrityError("MongoDB store initialization state is partial")
        if coordinator is None:
            raise IntegrityError("MongoDB store initialization state is partial")
        transaction = _MongoTransaction(
            self._records,
            session,
            self.store_id,
            timestamp=None,
        )
        transaction._validate(schema, _SCHEMA_BUCKET, _SCHEMA_KEY)
        version = schema.get("value")
        if version != _SCHEMA_VERSION:
            shown = "missing" if version is None else ascii(version)[1:-1]
            raise IntegrityError(
                f"unsupported MongoDB schema version: {shown}"
            )
        self._validate_coordinator(coordinator)
        return "existing"

    def _initialize_fresh_namespace(self) -> None:
        read_concern, write_concern, primary = self._transaction_options()
        return_document = self._pymongo.ReturnDocument.AFTER

        def initialize(session: Any) -> None:
            state = self._namespace_state(session)
            if state == "existing":
                return
            coordinator = self._coordinators.find_one_and_update(
                {"_id": self.store_id},
                [
                    {
                        "$set": {
                            "store_id": self.store_id,
                            "revision": 1,
                            "locked_at": "$$NOW",
                        }
                    }
                ],
                upsert=True,
                return_document=return_document,
                session=session,
            )
            self._validate_coordinator(coordinator)
            _MongoTransaction(
                self._records,
                session,
                self.store_id,
                timestamp=None,
            ).put(_SCHEMA_BUCKET, _SCHEMA_KEY, _SCHEMA_VERSION)

        with self._client.start_session() as session:
            session.with_transaction(
                initialize,
                read_concern=read_concern,
                write_concern=write_concern,
                read_preference=primary,
            )

    def _record_index_information(self) -> dict[str, Any]:
        try:
            information = self._records.index_information()
        except Exception as exc:
            if getattr(exc, "code", None) == 26:
                return {}
            raise
        if not isinstance(information, dict):
            raise IntegrityError("MongoDB record index metadata is invalid")
        return information

    @staticmethod
    def _matching_record_indexes(
        information: dict[str, Any],
    ) -> list[dict[str, Any]]:
        matching: list[dict[str, Any]] = []
        for details in information.values():
            if not isinstance(details, dict):
                raise IntegrityError("MongoDB record index metadata is invalid")
            keys = details.get("key")
            if not isinstance(keys, (list, tuple)):
                continue
            try:
                normalized = [tuple(item) for item in keys]
            except (TypeError, ValueError):
                continue
            if normalized == _RECORD_INDEX:
                matching.append(details)
            elif (
                normalized != [("_id", 1)]
                and details.get("unique") is True
            ):
                raise IntegrityError(
                    "MongoDB record unique index is missing or incompatible"
                )
        return matching

    @staticmethod
    def _validate_record_indexes(matching: list[dict[str, Any]]) -> None:
        if len(matching) != 1 or matching[0].get("unique") is not True:
            raise IntegrityError(
                "MongoDB record unique index is missing or incompatible"
            )
        if (
            matching[0].get("sparse") is True
            or "partialFilterExpression" in matching[0]
        ):
            raise IntegrityError(
                "MongoDB record unique index is missing or incompatible"
            )
        collation = matching[0].get("collation")
        if (
            collation is not None
            and (
                not isinstance(collation, dict)
                or collation.get("locale") != "simple"
            )
        ):
            raise IntegrityError(
                "MongoDB record unique index is missing or incompatible"
            )

    def _require_record_index(self) -> None:
        self._validate_record_indexes(
            self._matching_record_indexes(self._record_index_information())
        )

    def _ensure_record_index_for_fresh_namespace(self) -> None:
        matching = self._matching_record_indexes(
            self._record_index_information()
        )
        if matching:
            self._validate_record_indexes(matching)
            return
        self._records.create_index(
            _RECORD_INDEX,
            unique=True,
            name=_RECORD_INDEX_NAME,
        )
        self._require_record_index()

    def _validate_coordinator(self, coordinator: object) -> None:
        if not isinstance(coordinator, dict):
            raise IntegrityError("MongoDB coordinator is missing or corrupt")
        revision = coordinator.get("revision")
        locked_at = coordinator.get("locked_at")
        if (
            coordinator.get("_id") != self.store_id
            or coordinator.get("store_id") != self.store_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise IntegrityError("MongoDB coordinator collision or corruption")
        if locked_at is None or not hasattr(locked_at, "timestamp"):
            raise IntegrityError("MongoDB did not return its current time")

    def _transaction_options(self) -> tuple[Any, Any, Any]:
        read_concern = import_module("pymongo.read_concern").ReadConcern("snapshot")
        write_concern = import_module("pymongo.write_concern").WriteConcern("majority")
        return read_concern, write_concern, self._pymongo.ReadPreference.PRIMARY

    def _connection(self) -> _MongoConnection:
        return self._client, self._database, self._records, self._coordinators

    def _set_connection(self, connection: _MongoConnection) -> None:
        self._client, self._database, self._records, self._coordinators = connection

    def _read(self, callback: Callable[[KVTransaction], T]) -> T:
        read_concern, write_concern, primary = self._transaction_options()
        with (
            self._client.start_session() as session,
            session.start_transaction(
                read_concern=read_concern,
                write_concern=write_concern,
                read_preference=primary,
            ),
        ):
            return callback(
                _MongoTransaction(
                    self._records,
                    session,
                    self.store_id,
                    timestamp=None,
                )
            )

    def _write(self, callback: Callable[[KVTransaction], T]) -> T:
        read_concern, write_concern, primary = self._transaction_options()
        return_document = self._pymongo.ReturnDocument.AFTER

        def transaction(session: Any) -> T:
            current = self._coordinators.find_one(
                {"_id": self.store_id},
                session=session,
            )
            self._validate_coordinator(current)
            revision = current["revision"]
            coordinator = self._coordinators.find_one_and_update(
                {
                    "_id": self.store_id,
                    "store_id": self.store_id,
                    "revision": revision,
                },
                [
                    {
                        "$set": {
                            "revision": {"$add": ["$revision", 1]},
                            "locked_at": "$$NOW",
                        }
                    }
                ],
                upsert=False,
                return_document=return_document,
                session=session,
            )
            self._validate_coordinator(coordinator)
            locked_at = coordinator["locked_at"]
            return callback(
                _MongoTransaction(
                    self._records,
                    session,
                    self.store_id,
                    timestamp=float(locked_at.timestamp()),
                )
            )

        with self._client.start_session() as session:
            return cast(
                T,
                session.with_transaction(
                    transaction,
                    read_concern=read_concern,
                    write_concern=write_concern,
                    read_preference=primary,
                ),
            )

    def _is_connection_error(self, exc: BaseException) -> bool:
        errors = self._pymongo.errors
        return isinstance(
            exc,
            (
                errors.ConnectionFailure,
                errors.NetworkTimeout,
                errors.ServerSelectionTimeoutError,
                errors.WTimeoutError,
            ),
        )


class _MongoTransaction:
    def __init__(
        self,
        records: Any,
        session: Any,
        store_id: str,
        *,
        timestamp: float | None,
    ) -> None:
        self._records = records
        self._session = session
        self._store_id = store_id
        self._timestamp = timestamp

    def get(self, bucket: str, key: str) -> str | None:
        record_id = _record_id(self._store_id, bucket, key)
        record = self._records.find_one({"_id": record_id}, session=self._session)
        if record is None:
            return None
        self._validate(record, bucket, key)
        value = record.get("value")
        if not isinstance(value, str):
            raise IntegrityError("MongoDB Pollard record value must be a string")
        return value

    def items(self, bucket: str) -> list[tuple[str, str]]:
        cursor = self._records.find(
            {"store_id": self._store_id, "bucket": bucket},
            session=self._session,
        ).sort("key", 1)
        items: list[tuple[str, str]] = []
        for record in cursor:
            key = record.get("key")
            value = record.get("value")
            if not isinstance(key, str) or not isinstance(value, str):
                raise IntegrityError("invalid MongoDB Pollard record")
            self._validate(record, bucket, key)
            items.append((key, value))
        return items

    def put(self, bucket: str, key: str, value: str) -> None:
        record_id = _record_id(self._store_id, bucket, key)
        existing = self._records.find_one({"_id": record_id}, session=self._session)
        if existing is not None:
            self._validate(existing, bucket, key)
        self._records.replace_one(
            {"_id": record_id},
            {
                "_id": record_id,
                "store_id": self._store_id,
                "bucket": bucket,
                "key": key,
                "value": value,
            },
            upsert=True,
            session=self._session,
        )

    def delete(self, bucket: str, key: str) -> None:
        record_id = _record_id(self._store_id, bucket, key)
        existing = self._records.find_one({"_id": record_id}, session=self._session)
        if existing is not None:
            self._validate(existing, bucket, key)
        self._records.delete_one({"_id": record_id}, session=self._session)

    def now(self) -> float:
        if self._timestamp is not None:
            return self._timestamp
        result = list(
            self._records.aggregate(
                [{"$limit": 1}, {"$project": {"_id": 0, "now": "$$NOW"}}],
                session=self._session,
            )
        )
        if result:
            current = result[0].get("now")
        else:
            command = self._records.database.command("hello", session=self._session)
            current = command.get("localTime")
        if current is None or not hasattr(current, "timestamp"):
            raise IntegrityError("MongoDB did not return its current time")
        self._timestamp = float(current.timestamp())
        return self._timestamp

    def _validate(self, record: dict[str, Any], bucket: str, key: str) -> None:
        if (
            record.get("_id") != _record_id(self._store_id, bucket, key)
            or record.get("store_id") != self._store_id
            or record.get("bucket") != bucket
            or record.get("key") != key
        ):
            raise IntegrityError("MongoDB Pollard record collision or corruption")


def _record_id(store_id: str, bucket: str, key: str) -> str:
    return hashlib.sha256(canonical_bytes([store_id, bucket, key])).hexdigest()
