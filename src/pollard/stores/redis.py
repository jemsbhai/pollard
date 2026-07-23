"""Redis-backed transactional Pollard store."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from importlib import import_module
from typing import Any, TypeVar

from pollard.errors import IntegrityError

from ._transactional import KVTransaction, TransactionalKVStore

T = TypeVar("T")

_IDENTITY_BUCKET = "schema"
_IDENTITY_KEY = "redis-store-id"
_SCHEMA_VERSION_KEY = "version"
_SCHEMA_VERSION = "1"
_KNOWN_BUCKETS = (
    _IDENTITY_BUCKET,
    "nodes",
    "budget",
    "reservations",
    "window-events",
)


class RedisStore(TransactionalKVStore):
    """A logical Pollard store serialized through Redis transactions.

    Every key for one logical store includes the same Redis Cluster hash tag.
    Writes use ``WATCH``/``MULTI``/``EXEC`` around a per-store revision key,
    while all arithmetic remains in the shared Python implementation so exact
    decimal strings are never narrowed through Redis numeric operations.
    """

    backend_name = "Redis"

    def __init__(
        self,
        url: str | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        store_id: str = "default",
        prefix: str = "pollard",
        watch_retries: int = 64,
    ) -> None:
        if url is None and client_factory is None:
            raise ValueError("pass either url or client_factory")
        if url is not None and (not isinstance(url, str) or not url):
            raise ValueError("url must be a non-empty string")
        if url is not None and client_factory is not None:
            raise ValueError("pass either url or client_factory, not both")
        if client_factory is not None and not callable(client_factory):
            raise ValueError("client_factory must be callable")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("prefix must be a non-empty string")
        if isinstance(watch_retries, bool) or not isinstance(watch_retries, int):
            raise ValueError("watch_retries must be a positive integer")
        if watch_retries < 1:
            raise ValueError("watch_retries must be a positive integer")
        try:
            redis = import_module("redis")
        except ImportError as exc:
            raise ImportError(
                "RedisStore requires the 'redis' extra: "
                "pip install 'pollard[redis]'"
            ) from exc

        self.url = url
        self._client_factory = client_factory
        self.store_id = store_id
        self.prefix = prefix
        self.watch_retries = watch_retries
        self._redis = redis
        self._watch_error = redis.exceptions.WatchError
        self._connection_errors = _connection_error_types(redis)
        tag = hashlib.sha256(store_id.encode("utf-8")).hexdigest()
        # The hash tag comes first, so braces supplied in ``prefix`` cannot
        # accidentally select a different Redis Cluster slot.
        self._base_key = f"{{pollard-{tag}}}:{prefix}"
        self._revision_key = f"{self._base_key}:revision"
        self._initialized = False
        self._closed = False
        self._client: Any = self._new_client()
        try:
            self._client.ping()
            self._initialize_identity()
            self._initialize_transactional_store()
            self._initialized = True
        except BaseException:
            self._close_client(self._client)
            self._closed = True
            raise

    def __enter__(self) -> RedisStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the Redis client and its connection pool."""

        if self._closed:
            return
        self._close_client(self._client)
        self._closed = True

    def reconnect(self) -> None:
        """Replace the client and fail closed if store identity changed."""

        previous = self._client
        was_closed = self._closed
        replacement = self._new_client()
        if replacement is previous:
            raise RuntimeError("client_factory must return a fresh Redis client")
        self._client = replacement
        self._closed = False
        try:
            replacement.ping()
            self._require_identity_and_schema()
        except BaseException:
            self._close_client(replacement)
            self._client = previous
            self._closed = was_closed
            raise
        self._close_client(previous)

    def _read(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._run_transaction(callback, allow_writes=False)

    def _write(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._run_transaction(callback, allow_writes=True)

    def _run_transaction(
        self,
        callback: Callable[[KVTransaction], T],
        *,
        allow_writes: bool,
    ) -> T:
        if self._closed:
            raise RuntimeError("RedisStore is closed; call reconnect() before use")
        last_conflict: BaseException | None = None
        for _attempt in range(self.watch_retries):
            try:
                with self._client.pipeline(transaction=True) as pipe:
                    pipe.watch(self._revision_key)
                    revision = pipe.get(self._revision_key)
                    self._validate_revision(revision)
                    transaction = _RedisTransaction(
                        pipe,
                        self._bucket_key,
                        _server_time(pipe.time()),
                    )
                    result = callback(transaction)
                    if not allow_writes and transaction.has_writes:
                        raise RuntimeError("Redis read transaction attempted a write")
                    if revision is None and not transaction.has_writes:
                        raise IntegrityError("Redis transaction revision is missing")

                    # No mutation is sent before MULTI. Even a read-only or
                    # idempotent callback queues a GET so EXEC verifies WATCH.
                    pipe.multi()
                    transaction.queue_writes(pipe)
                    if transaction.has_writes:
                        pipe.incr(self._revision_key)
                    else:
                        pipe.get(self._revision_key)
                    pipe.execute()
                    return result
            except self._watch_error as exc:
                last_conflict = exc
                continue
        raise RuntimeError(
            "Redis transaction could not commit after "
            f"{self.watch_retries} WATCH conflicts"
        ) from last_conflict

    def _is_connection_error(self, exc: BaseException) -> bool:
        return isinstance(exc, self._connection_errors)

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        return self._redis.Redis.from_url(self.url, decode_responses=True)

    def _bucket_key(self, bucket: str) -> str:
        return f"{self._base_key}:bucket:{bucket}"

    def _validate_revision(self, revision: object) -> None:
        if revision is None:
            if self._initialized:
                raise IntegrityError("Redis transaction revision is missing")
            return
        if not isinstance(revision, str):
            raise IntegrityError("Redis transaction revision must be a string")
        try:
            value = int(revision)
        except ValueError as exc:
            raise IntegrityError("Redis transaction revision is invalid") from exc
        if value < 1 or str(value) != revision:
            raise IntegrityError("Redis transaction revision is invalid")

    def _initialize_identity(self) -> None:
        def initialize(tx: KVTransaction) -> None:
            identity = tx.get(_IDENTITY_BUCKET, _IDENTITY_KEY)
            if identity is None:
                if any(tx.items(bucket) for bucket in _KNOWN_BUCKETS):
                    raise IntegrityError("Redis store identity is missing")
                tx.put(_IDENTITY_BUCKET, _IDENTITY_KEY, self.store_id)
                return
            if identity != self.store_id:
                raise IntegrityError("Redis store identity does not match store_id")

        self._write(initialize)

    def _require_identity_and_schema(self) -> None:
        def validate(tx: KVTransaction) -> None:
            identity = tx.get(_IDENTITY_BUCKET, _IDENTITY_KEY)
            if identity != self.store_id:
                raise IntegrityError("Redis store identity does not match store_id")
            version = tx.get(_IDENTITY_BUCKET, _SCHEMA_VERSION_KEY)
            if version != _SCHEMA_VERSION:
                shown = "missing" if version is None else version
                raise IntegrityError(
                    f"unsupported Redis schema version: {shown}"
                )

        self._read(validate)

    @staticmethod
    def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()


class _RedisTransaction:
    """Buffered key/value view over one watched Redis pipeline."""

    def __init__(
        self,
        pipe: Any,
        bucket_key: Callable[[str], str],
        now: float,
    ) -> None:
        self._pipe = pipe
        self._bucket_key = bucket_key
        self._now = now
        self._pending: dict[tuple[str, str], str | None] = {}

    @property
    def has_writes(self) -> bool:
        return bool(self._pending)

    def get(self, bucket: str, key: str) -> str | None:
        pending_key = (bucket, key)
        if pending_key in self._pending:
            return self._pending[pending_key]
        value = self._pipe.hget(self._bucket_key(bucket), key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise IntegrityError("Redis stored value must be a string")
        return value

    def items(self, bucket: str) -> list[tuple[str, str]]:
        raw = self._pipe.hgetall(self._bucket_key(bucket))
        if not isinstance(raw, dict):
            raise IntegrityError("Redis bucket must be a hash")
        values: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise IntegrityError("Redis bucket entries must be strings")
            values[key] = value
        for (pending_bucket, key), value in self._pending.items():
            if pending_bucket != bucket:
                continue
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        return sorted(values.items())

    def put(self, bucket: str, key: str, value: str) -> None:
        if not all(isinstance(item, str) for item in (bucket, key, value)):
            raise TypeError("Redis transaction keys and values must be strings")
        self._pending[(bucket, key)] = value

    def delete(self, bucket: str, key: str) -> None:
        if not isinstance(bucket, str) or not isinstance(key, str):
            raise TypeError("Redis transaction keys must be strings")
        self._pending[(bucket, key)] = None

    def now(self) -> float:
        return self._now

    def queue_writes(self, pipe: Any) -> None:
        for (bucket, key), value in self._pending.items():
            bucket_key = self._bucket_key(bucket)
            if value is None:
                pipe.hdel(bucket_key, key)
            else:
                pipe.hset(bucket_key, key, value)


def _server_time(value: object) -> float:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise IntegrityError("Redis TIME returned an invalid value")
    seconds, microseconds = value
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise IntegrityError("Redis TIME returned invalid seconds")
    if isinstance(microseconds, bool) or not isinstance(microseconds, int):
        raise IntegrityError("Redis TIME returned invalid microseconds")
    if seconds < 0 or not 0 <= microseconds < 1_000_000:
        raise IntegrityError("Redis TIME returned an invalid value")
    return float(seconds) + (float(microseconds) / 1_000_000)


def _connection_error_types(redis: Any) -> tuple[type[BaseException], ...]:
    names = (
        "ConnectionError",
        "TimeoutError",
        "BusyLoadingError",
        "ClusterDownError",
        "MasterDownError",
        "ReadOnlyError",
    )
    values = [getattr(redis.exceptions, name, None) for name in names]
    return tuple(
        value
        for value in values
        if isinstance(value, type) and issubclass(value, BaseException)
    )
