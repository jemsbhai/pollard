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


class MongoStore(TransactionalKVStore):
    """A transactional logical Pollard store in MongoDB.

    MongoDB transactions require a replica set or sharded deployment. A
    standalone server is refused instead of silently weakening accounting.
    """

    backend_name = "MongoDB"

    def __init__(
        self,
        uri: str,
        *,
        database: str = "pollard",
        store_id: str = "default",
        collection_prefix: str = "pollard",
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
        self._client_options = dict(client_options)
        self._pymongo = pymongo
        self._client: Any = None
        self._database: Any = None
        self._records: Any = None
        self._coordinators: Any = None
        self._open()
        try:
            self._initialize_transactional_store()
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
        self.close()
        self._open()
        self._require_transactional_store()

    def _open(self) -> None:
        self._client = self._pymongo.MongoClient(self.uri, **self._client_options)
        try:
            topology = self._client.admin.command("hello")
            if topology.get("setName") is None and topology.get("msg") != "isdbgrid":
                raise ValueError(
                    "MongoStore requires a replica set or sharded MongoDB deployment"
                )
            self._database = self._client[self.database_name]
            self._records = self._database[f"{self.collection_prefix}_records"]
            self._coordinators = self._database[
                f"{self.collection_prefix}_coordinators"
            ]
            self._records.create_index(
                [("store_id", 1), ("bucket", 1), ("key", 1)], unique=True
            )
        except BaseException:
            self._client.close()
            raise

    def _read(self, callback: Callable[[KVTransaction], T]) -> T:
        read_concern = import_module("pymongo.read_concern").ReadConcern("snapshot")
        write_concern = import_module("pymongo.write_concern").WriteConcern("majority")
        primary = self._pymongo.ReadPreference.PRIMARY
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
        read_concern = import_module("pymongo.read_concern").ReadConcern("snapshot")
        write_concern = import_module("pymongo.write_concern").WriteConcern("majority")
        primary = self._pymongo.ReadPreference.PRIMARY
        return_document = self._pymongo.ReturnDocument.AFTER

        def transaction(session: Any) -> T:
            coordinator = self._coordinators.find_one_and_update(
                {"_id": self.store_id},
                [
                    {
                        "$set": {
                            "store_id": self.store_id,
                            "revision": {
                                "$add": [{"$ifNull": ["$revision", 0]}, 1]
                            },
                            "locked_at": "$$NOW",
                        }
                    }
                ],
                upsert=True,
                return_document=return_document,
                session=session,
            )
            if coordinator is None or coordinator.get("store_id") != self.store_id:
                raise IntegrityError("MongoDB coordinator collision or corruption")
            locked_at = coordinator.get("locked_at")
            if locked_at is None or not hasattr(locked_at, "timestamp"):
                raise IntegrityError("MongoDB did not return its current time")
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
