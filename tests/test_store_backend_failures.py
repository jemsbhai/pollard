from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from operator import attrgetter
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

import pollard.stores.kafka as kafka_module
import pollard.stores.mongodb as mongodb_module
import pollard.stores.neo4j as neo4j_module
import pollard.stores.redis as redis_module
from pollard.arbiter import WindowReservation
from pollard.errors import IntegrityError, ReservationUncertain
from pollard.stores._transactional import _compound_key
from pollard.stores.kafka import KafkaStore
from pollard.stores.redis import _server_time
from pollard.tree import Node, NodeKind

from .test_store_transactional import _budget, _ConnectionLost, _FakeStore


@pytest.mark.parametrize(
    ("args", "kwargs", "error"),
    [
        ((None,), {"topic": "t"}, TypeError),
        (({},), {"topic": ""}, ValueError),
        (({},), {"topic": "t", "store_id": ""}, ValueError),
        (({},), {"topic": "t", "timeout": True}, ValueError),
        (({},), {"topic": "t", "timeout": 0}, ValueError),
        (({},), {"topic": "t"}, ValueError),
        (
            ({"bootstrap.servers": "unused", "transactional.id": "wrong"},),
            {"topic": "t"},
            ValueError,
        ),
    ],
)
def test_kafka_constructor_validation(
    args: tuple[object, ...], kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        KafkaStore(*args, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("constructor", "args", "kwargs"),
    [
        (redis_module.RedisStore, ("redis://unused",), {}),
        (mongodb_module.MongoStore, ("mongodb://unused",), {}),
        (neo4j_module.Neo4jStore, ("bolt://unused", None), {}),
        (
            kafka_module.KafkaStore,
            ({"bootstrap.servers": "unused"},),
            {"topic": "unused"},
        ),
    ],
)
def test_optional_backend_import_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
    constructor: Any,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    module = __import__(constructor.__module__, fromlist=["unused"])

    def missing(_name: str) -> NoReturn:
        raise ImportError("missing")

    monkeypatch.setattr(module, "import_module", missing)
    with pytest.raises(ImportError, match=r"pollard\["):
        constructor(*args, **kwargs)


@pytest.mark.parametrize(
    ("constructor", "args", "kwargs", "message"),
    [
        (redis_module.RedisStore, (), {}, "either url or client_factory"),
        (redis_module.RedisStore, ("",), {}, "url"),
        (
            redis_module.RedisStore,
            ("redis://unused",),
            {"client_factory": lambda: object()},
            "not both",
        ),
        (
            redis_module.RedisStore,
            (),
            {"client_factory": 1},
            "client_factory",
        ),
        (redis_module.RedisStore, ("redis://unused",), {"store_id": ""}, "store_id"),
        (redis_module.RedisStore, ("redis://unused",), {"prefix": ""}, "prefix"),
        (
            redis_module.RedisStore,
            ("redis://unused",),
            {"watch_retries": False},
            "watch_retries",
        ),
        (mongodb_module.MongoStore, ("",), {}, "uri"),
        (
            mongodb_module.MongoStore,
            ("mongodb://unused",),
            {"database": ""},
            "database",
        ),
        (
            mongodb_module.MongoStore,
            ("mongodb://unused",),
            {"collection_prefix": "bad-name"},
            "collection_prefix",
        ),
        (neo4j_module.Neo4jStore, ("", None), {}, "uri"),
        (
            neo4j_module.Neo4jStore,
            ("bolt://unused", None),
            {"database": ""},
            "database",
        ),
        (
            neo4j_module.Neo4jStore,
            ("bolt://unused", None),
            {"store_id": ""},
            "store_id",
        ),
    ],
)
def test_remote_constructor_validation(
    constructor: Any,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        constructor(*args, **kwargs)


def test_redis_constructor_requires_boolean_create() -> None:
    with pytest.raises(TypeError, match="create must be a boolean"):
        redis_module.RedisStore("redis://unused", create=1)  # type: ignore[arg-type]


def test_mongodb_constructor_requires_boolean_create() -> None:
    with pytest.raises(TypeError, match="create must be a boolean"):
        mongodb_module.MongoStore(  # type: ignore[arg-type]
            "mongodb://unused",
            create=1,
        )


def test_neo4j_constructor_requires_boolean_create() -> None:
    with pytest.raises(TypeError, match="create must be a boolean"):
        neo4j_module.Neo4jStore(  # type: ignore[arg-type]
            "bolt://unused",
            None,
            create=1,
        )


@pytest.mark.parametrize(
    "value",
    [None, [1], [1, 2, 3], [True, 1], [1, True], [-1, 0], [1, -1], [1, 1_000_000]],
)
def test_redis_server_time_fails_closed(value: object) -> None:
    with pytest.raises(IntegrityError, match="TIME"):
        _server_time(value)


def test_redis_client_factory_owns_construction_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.closed = False

        def ping(self) -> bool:
            return True

        def close(self) -> None:
            self.closed = True

    clients = [Client(), Client()]
    created: list[Client] = []

    def factory() -> Client:
        client = clients[len(created)]
        created.append(client)
        return client

    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
        Redis=SimpleNamespace(
            from_url=lambda *_args, **_kwargs: pytest.fail("URL client was used")
        ),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_identity",
        lambda _self: None,
    )
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_transactional_store",
        lambda _self: None,
    )
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_require_identity_and_schema",
        lambda _self: None,
    )

    store = redis_module.RedisStore(client_factory=factory)
    assert store.url is None
    assert store._client is clients[0]
    store.reconnect()
    assert clients[0].closed
    assert store._client is clients[1]
    assert not clients[1].closed
    store.close()
    assert clients[1].closed


def test_redis_client_factory_must_return_fresh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(ping=lambda: True, close=lambda: None)
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
        Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_identity",
        lambda _self: None,
    )
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_transactional_store",
        lambda _self: None,
    )

    store = redis_module.RedisStore(client_factory=lambda: client)
    with pytest.raises(RuntimeError, match="fresh Redis client"):
        store.reconnect()


@pytest.mark.parametrize(
    ("identity", "schema", "revision", "message"),
    [
        ("store", "1", "7", None),
        (None, "1", "7", "identity is missing"),
        ("other", "1", "7", "identity does not match"),
        ("store", None, "7", "schema version: missing"),
        ("store", "999", "7", "schema version: 999"),
        ("store", "1", None, "revision is missing"),
        ("store", "1", "invalid", "revision is invalid"),
    ],
)
def test_redis_create_false_requires_existing_valid_store_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    identity: str | None,
    schema: str | None,
    revision: str | None,
    message: str | None,
) -> None:
    class Pipe:
        def __init__(self) -> None:
            self.revision_reads = 0
            self.hash_reads: list[str] = []
            self.writes: list[str] = []

        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def watch(self, _key: str) -> None:
            return None

        def get(self, _key: str) -> str | None:
            self.revision_reads += 1
            return revision

        def time(self) -> list[int]:
            return [1, 0]

        def hget(self, _bucket: str, key: str) -> str | None:
            self.hash_reads.append(key)
            if key == "redis-store-id":
                return identity
            if key == "version":
                return schema
            pytest.fail(f"unexpected Redis hash read: {key}")

        def multi(self) -> None:
            return None

        def execute(self) -> list[object]:
            return []

        def hset(self, *_args: object) -> None:
            self.writes.append("hset")

        def hdel(self, *_args: object) -> None:
            self.writes.append("hdel")

        def incr(self, *_args: object) -> None:
            self.writes.append("incr")

    class Client:
        def __init__(self, pipe: Pipe) -> None:
            self.pipe = pipe
            self.closed = False

        def ping(self) -> bool:
            return True

        def pipeline(self, *, transaction: bool) -> Pipe:
            assert transaction
            return self.pipe

        def close(self) -> None:
            self.closed = True

    pipe = Pipe()
    client = Client(pipe)
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_identity",
        lambda _self: pytest.fail("create=False initialized Redis identity"),
    )
    monkeypatch.setattr(
        redis_module.RedisStore,
        "_initialize_transactional_store",
        lambda _self: pytest.fail("create=False initialized Redis schema"),
    )

    if message is None:
        with redis_module.RedisStore(
            client_factory=lambda: client,
            store_id="store",
            create=False,
        ) as store:
            assert store.create is False
            assert pipe.revision_reads == 2
            assert pipe.hash_reads == ["redis-store-id", "version"]
    else:
        with pytest.raises(IntegrityError, match=message):
            redis_module.RedisStore(
                client_factory=lambda: client,
                store_id="store",
                create=False,
            )

    assert not pipe.writes
    assert client.closed


def test_redis_creation_commits_identity_schema_and_revision_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipe:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []
            self.execute_calls = 0

        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def watch(self, key: str) -> None:
            self.commands.append(("watch", key))

        def get(self, key: str) -> None:
            self.commands.append(("get", key))
            return None

        def time(self) -> list[int]:
            return [1, 0]

        def hget(self, bucket: str, key: str) -> None:
            self.commands.append(("hget", bucket, key))
            return None

        def hgetall(self, bucket: str) -> dict[str, str]:
            self.commands.append(("hgetall", bucket))
            return {}

        def multi(self) -> None:
            self.commands.append(("multi",))

        def hset(self, bucket: str, key: str, value: str) -> None:
            self.commands.append(("hset", bucket, key, value))

        def hdel(self, *_args: object) -> None:
            pytest.fail("creation deleted a Redis value")

        def incr(self, key: str) -> None:
            self.commands.append(("incr", key))

        def execute(self) -> list[object]:
            self.execute_calls += 1
            self.commands.append(("execute",))
            return []

    class Client:
        def __init__(self) -> None:
            self.pipe = Pipe()
            self.closed = False

        def ping(self) -> bool:
            return True

        def pipeline(self, *, transaction: bool) -> Pipe:
            assert transaction
            return self.pipe

        def close(self) -> None:
            self.closed = True

    client = Client()
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)

    with redis_module.RedisStore(
        client_factory=lambda: client,
        store_id="store",
        prefix="tenant",
    ):
        pass

    pipe = client.pipe
    assert pipe.execute_calls == 1
    multi_index = pipe.commands.index(("multi",))
    execute_index = pipe.commands.index(("execute",))
    queued = pipe.commands[multi_index + 1 : execute_index]
    assert [command[0] for command in queued] == ["hset", "hset", "incr"]
    assert queued[0][2:] == ("redis-store-id", "store")
    assert queued[1][2:] == ("version", "1")
    assert queued[0][1] == queued[1][1]
    assert queued[2][1].endswith(":revision")
    assert client.closed


def test_redis_creation_reuses_valid_namespace_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipe:
        def __init__(self) -> None:
            self.revision_reads = 0
            self.writes: list[str] = []
            self.execute_calls = 0

        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def watch(self, _key: str) -> None:
            return None

        def get(self, _key: str) -> str:
            self.revision_reads += 1
            return "7"

        def time(self) -> list[int]:
            return [1, 0]

        def hget(self, _bucket: str, key: str) -> str:
            return "store" if key == "redis-store-id" else "1"

        def multi(self) -> None:
            return None

        def hset(self, *_args: object) -> None:
            self.writes.append("hset")

        def hdel(self, *_args: object) -> None:
            self.writes.append("hdel")

        def incr(self, *_args: object) -> None:
            self.writes.append("incr")

        def execute(self) -> list[object]:
            self.execute_calls += 1
            return []

    class Client:
        def __init__(self) -> None:
            self.pipe = Pipe()

        def ping(self) -> bool:
            return True

        def pipeline(self, *, transaction: bool) -> Pipe:
            assert transaction
            return self.pipe

        def close(self) -> None:
            return None

    client = Client()
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)

    with redis_module.RedisStore(
        client_factory=lambda: client,
        store_id="store",
        prefix="tenant",
    ):
        pass

    assert client.pipe.execute_calls == 1
    assert client.pipe.revision_reads == 2
    assert not client.pipe.writes


@pytest.mark.parametrize(
    ("revision", "identity", "schema", "message"),
    [
        ("7", None, None, "identity is missing"),
        (None, "store", None, "initialization state is partial"),
        (None, None, "1", "initialization state is partial"),
        (None, "store", "1", "initialization state is partial"),
        ("7", "store", None, "schema version: missing"),
    ],
    ids=[
        "revision-only",
        "identity-only",
        "schema-only",
        "identity-schema",
        "identity-revision",
    ],
)
def test_redis_creation_rejects_partial_namespace_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    revision: str | None,
    identity: str | None,
    schema: str | None,
    message: str,
) -> None:
    class Pipe:
        def __init__(self) -> None:
            self.writes: list[str] = []
            self.execute_calls = 0

        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def watch(self, _key: str) -> None:
            return None

        def get(self, _key: str) -> str | None:
            return revision

        def time(self) -> list[int]:
            return [1, 0]

        def hget(self, _bucket: str, key: str) -> str | None:
            if key == "redis-store-id":
                return identity
            if key == "version":
                return schema
            pytest.fail(f"unexpected Redis hash read: {key}")

        def hgetall(self, bucket: str) -> dict[str, str]:
            if not bucket.endswith(":bucket:schema"):
                return {}
            return {
                key: value
                for key, value in (
                    ("redis-store-id", identity),
                    ("version", schema),
                )
                if value is not None
            }

        def multi(self) -> None:
            self.writes.append("multi")

        def hset(self, *_args: object) -> None:
            self.writes.append("hset")

        def hdel(self, *_args: object) -> None:
            self.writes.append("hdel")

        def incr(self, *_args: object) -> None:
            self.writes.append("incr")

        def execute(self) -> list[object]:
            self.execute_calls += 1
            return []

    class Client:
        def __init__(self) -> None:
            self.pipe = Pipe()
            self.closed = False

        def ping(self) -> bool:
            return True

        def pipeline(self, *, transaction: bool) -> Pipe:
            assert transaction
            return self.pipe

        def close(self) -> None:
            self.closed = True

    client = Client()
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)

    with pytest.raises(IntegrityError, match=message):
        redis_module.RedisStore(
            client_factory=lambda: client,
            store_id="store",
            prefix="tenant",
            create=True,
        )

    assert not client.pipe.writes
    assert client.pipe.execute_calls == 0
    assert client.closed


@pytest.mark.parametrize(
    "query",
    [
        "decode_responses=false",
        "encoding=utf-16",
        "encoding_errors=ignore",
    ],
)
def test_redis_url_rejects_text_overrides_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    def unexpected_import(_name: str) -> object:
        raise AssertionError("Redis client module was imported")

    monkeypatch.setattr(redis_module, "import_module", unexpected_import)
    url = (
        "rediss://private-user:private-password@redis.example/0"
        f"?{query}"
    )

    with pytest.raises(
        ValueError,
        match="must not override Pollard text decoding options",
    ) as caught:
        redis_module.RedisStore(url)

    message = str(caught.value)
    for secret in (url, "private-user", "private-password"):
        assert secret not in message


def test_redis_corrupt_schema_version_is_safely_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt_version = "999\r\n\x1b\u2028"

    class Pipe:
        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def watch(self, _key: str) -> None:
            return None

        def get(self, _key: str) -> str:
            return "7"

        def time(self) -> list[int]:
            return [1, 0]

        def hget(self, _bucket: str, key: str) -> str:
            return "store" if key == "redis-store-id" else corrupt_version

    class Client:
        def __init__(self) -> None:
            self.closed = False

        def ping(self) -> bool:
            return True

        def pipeline(self, *, transaction: bool) -> Pipe:
            assert transaction
            return Pipe()

        def close(self) -> None:
            self.closed = True

    client = Client()
    fake_redis = SimpleNamespace(
        exceptions=SimpleNamespace(WatchError=RuntimeError),
    )
    monkeypatch.setattr(redis_module, "import_module", lambda _name: fake_redis)

    with pytest.raises(IntegrityError) as caught:
        redis_module.RedisStore(
            client_factory=lambda: client,
            store_id="store",
            create=False,
        )

    message = str(caught.value)
    assert message == r"unsupported Redis schema version: 999\r\n\x1b\u2028"
    assert all(character not in message for character in "\r\n\x1b\u2028")
    assert client.closed


def _install_read_only_mongodb(
    monkeypatch: pytest.MonkeyPatch,
    schema_versions: list[str | None],
) -> list[Any]:
    clients: list[Any] = []

    class Transaction:
        def __enter__(self) -> Transaction:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.read_transactions: list[dict[str, object]] = []
            self.write_transactions: list[dict[str, object]] = []

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def start_transaction(self, **options: object) -> Transaction:
            self.read_transactions.append(options)
            return Transaction()

        def with_transaction(
            self,
            callback: Callable[[Session], object],
            **options: object,
        ) -> object:
            self.write_transactions.append(options)
            return callback(self)

    class Records:
        def __init__(self, version: str | None) -> None:
            self.version = version
            self.schema_reads = 0
            self.index_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
            self.write_calls: list[str] = []

        def create_index(self, *args: object, **kwargs: object) -> None:
            self.index_calls.append((args, kwargs))

        def find_one(
            self,
            query: dict[str, object],
            *,
            session: Session,
        ) -> dict[str, object] | None:
            assert isinstance(session, Session)
            self.schema_reads += 1
            assert query in (
                {
                    "_id": mongodb_module._record_id(
                        "default", "schema", "version"
                    )
                },
                {"store_id": "default"},
            )
            if self.version is None:
                return None
            return {
                "_id": mongodb_module._record_id("default", "schema", "version"),
                "store_id": "default",
                "bucket": "schema",
                "key": "version",
                "value": self.version,
            }

        def index_information(self) -> dict[str, dict[str, object]]:
            return {
                "_id_": {"key": [("_id", 1)]},
                "pollard_unique": {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": True,
                },
            }

        def replace_one(self, *_args: object, **_kwargs: object) -> None:
            self.write_calls.append("replace_one")

        def delete_one(self, *_args: object, **_kwargs: object) -> None:
            self.write_calls.append("delete_one")

    class Coordinators:
        def __init__(self, exists: bool) -> None:
            self.exists = exists
            self.write_calls: list[str] = []

        def find_one(
            self,
            query: dict[str, object],
            *,
            session: Session,
        ) -> dict[str, object] | None:
            assert query == {"_id": "default"}
            assert isinstance(session, Session)
            if not self.exists:
                return None
            return {
                "_id": "default",
                "store_id": "default",
                "revision": 1,
                "locked_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }

        def find_one_and_update(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            self.write_calls.append("find_one_and_update")
            return {
                "_id": "default",
                "store_id": "default",
                "revision": 2,
                "locked_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }

    class Database:
        def __init__(self, records: Records, coordinators: Coordinators) -> None:
            self.records = records
            self.coordinators = coordinators

        def __getitem__(self, name: str) -> object:
            if name == "pollard_records":
                return self.records
            if name == "pollard_coordinators":
                return self.coordinators
            pytest.fail(f"unexpected MongoDB collection: {name}")

    class Client:
        def __init__(self, version: str | None) -> None:
            self.records = Records(version)
            self.coordinators = Coordinators(version is not None)
            self.database = Database(self.records, self.coordinators)
            self.admin = SimpleNamespace(command=self._admin_command)
            self.closed = False
            self.hello_calls = 0
            self.sessions: list[Session] = []

        def _admin_command(self, name: str) -> dict[str, str]:
            assert name == "hello"
            self.hello_calls += 1
            return {"setName": "rs0"}

        def __getitem__(self, name: str) -> Database:
            assert name == "pollard"
            return self.database

        def start_session(self) -> Session:
            session = Session()
            self.sessions.append(session)
            return session

        def close(self) -> None:
            self.closed = True

    def mongo_client(*_args: object, **_kwargs: object) -> Client:
        client = Client(schema_versions[len(clients)])
        clients.append(client)
        return client

    fake_pymongo = SimpleNamespace(
        MongoClient=mongo_client,
        ReadPreference=SimpleNamespace(PRIMARY="primary"),
        ReturnDocument=SimpleNamespace(AFTER="after"),
    )

    def import_fake(name: str) -> object:
        if name == "pymongo":
            return fake_pymongo
        if name == "pymongo.read_concern":
            return SimpleNamespace(ReadConcern=lambda value: ("read", value))
        if name == "pymongo.write_concern":
            return SimpleNamespace(WriteConcern=lambda value: ("write", value))
        pytest.fail(f"unexpected MongoDB import: {name}")

    monkeypatch.setattr(mongodb_module, "import_module", import_fake)
    return clients


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        ("1", None),
        (None, "schema version: missing"),
        ("999", "schema version: 999"),
    ],
)
def test_mongodb_create_false_requires_existing_schema_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str | None,
    message: str | None,
) -> None:
    clients = _install_read_only_mongodb(monkeypatch, [schema_version])

    if message is None:
        with mongodb_module.MongoStore("mongodb://unused", create=False) as store:
            assert store.create is False
    else:
        with pytest.raises(IntegrityError, match=message):
            mongodb_module.MongoStore("mongodb://unused", create=False)

    assert len(clients) == 1
    client = clients[0]
    assert client.hello_calls == 1
    assert client.records.schema_reads == (2 if schema_version is None else 1)
    assert not client.records.index_calls
    assert not client.records.write_calls
    assert not client.coordinators.write_calls
    assert len(client.sessions) == 1
    assert len(client.sessions[0].read_transactions) == 1
    assert not client.sessions[0].write_transactions
    assert client.closed


def test_mongodb_reconnect_preserves_create_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _install_read_only_mongodb(monkeypatch, ["1", "1"])

    store = mongodb_module.MongoStore("mongodb://unused", create=False)
    previous = clients[0]
    store.reconnect()
    replacement = clients[1]

    assert store.create is False
    assert store._client is replacement
    assert previous.closed
    assert not replacement.closed
    for client in clients:
        assert client.hello_calls == 1
        assert client.records.schema_reads == 1
        assert not client.records.index_calls
        assert not client.records.write_calls
        assert not client.coordinators.write_calls
        assert len(client.sessions) == 1
        assert len(client.sessions[0].read_transactions) == 1
        assert not client.sessions[0].write_transactions

    store.close()
    assert replacement.closed


def test_mongodb_reconnect_keeps_previous_client_until_schema_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    previous_client = Client()
    replacement_client = Client()
    previous = (previous_client, "old-db", "old-records", "old-coordinators")
    replacement = (
        replacement_client,
        "new-db",
        "new-records",
        "new-coordinators",
    )
    store = object.__new__(mongodb_module.MongoStore)
    store._set_connection(previous)
    monkeypatch.setattr(store, "_connect", lambda: replacement)

    def invalid_schema() -> NoReturn:
        raise IntegrityError("unsupported MongoDB schema version: 999")

    monkeypatch.setattr(store, "_require_existing_store", invalid_schema)
    with pytest.raises(IntegrityError, match="schema version"):
        store.reconnect()
    assert store._connection() == previous
    assert not previous_client.closed
    assert replacement_client.closed

    replacement_client.closed = False
    monkeypatch.setattr(store, "_require_existing_store", lambda: None)
    store.reconnect()
    assert store._connection() == replacement
    assert previous_client.closed
    assert not replacement_client.closed


def test_mongodb_connect_refuses_standalone_and_closes_candidate() -> None:
    class Client:
        closed = False
        admin = SimpleNamespace(command=lambda _name: {})

        def close(self) -> None:
            self.closed = True

    client = Client()
    store = object.__new__(mongodb_module.MongoStore)
    store.uri = "mongodb://unused"
    store.database_name = "pollard"
    store.collection_prefix = "pollard"
    store._client_options = {}
    store._pymongo = SimpleNamespace(MongoClient=lambda *_args, **_kwargs: client)

    with pytest.raises(ValueError, match="replica set or sharded"):
        store._connect()
    assert client.closed


@pytest.mark.parametrize(
    ("state", "expected", "message"),
    [
        ("fresh", "fresh", None),
        ("valid", "existing", None),
        ("schema-only", None, "initialization state is partial"),
        ("coordinator-only", None, "initialization state is partial"),
        ("data-only", None, "initialization state is partial"),
        ("future-schema", None, "schema version: 999"),
        ("bad-revision", None, "coordinator collision or corruption"),
        ("bad-identity", None, "coordinator collision or corruption"),
    ],
)
def test_mongodb_namespace_states_fail_closed(
    state: str,
    expected: str | None,
    message: str | None,
) -> None:
    schema_id = mongodb_module._record_id("store", "schema", "version")
    schema = {
        "_id": schema_id,
        "store_id": "store",
        "bucket": "schema",
        "key": "version",
        "value": "999" if state == "future-schema" else "1",
    }
    data = {
        "_id": mongodb_module._record_id("store", "nodes", "node"),
        "store_id": "store",
        "bucket": "nodes",
        "key": "node",
        "value": "{}",
    }
    coordinator = {
        "_id": "store",
        "store_id": "wrong" if state == "bad-identity" else "store",
        "revision": 0 if state == "bad-revision" else 1,
        "locked_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }

    class Records:
        def find_one(
            self,
            query: dict[str, object],
            *,
            session: object,
        ) -> dict[str, object] | None:
            assert session == "session"
            if "_id" in query:
                return (
                    schema
                    if state
                    in {
                        "valid",
                        "schema-only",
                        "future-schema",
                        "bad-revision",
                        "bad-identity",
                    }
                    else None
                )
            return data if state == "data-only" else None

    class Coordinators:
        def find_one(
            self,
            query: dict[str, object],
            *,
            session: object,
        ) -> dict[str, object] | None:
            assert query == {"_id": "store"}
            assert session == "session"
            return (
                coordinator
                if state
                in {
                    "valid",
                    "coordinator-only",
                    "future-schema",
                    "bad-revision",
                    "bad-identity",
                }
                else None
            )

    store = object.__new__(mongodb_module.MongoStore)
    store.store_id = "store"
    store._records = Records()
    store._coordinators = Coordinators()

    if message is None:
        assert store._namespace_state("session") == expected
    else:
        with pytest.raises(IntegrityError, match=message):
            store._namespace_state("session")


@pytest.mark.parametrize(
    ("details", "message"),
    [
        ({}, "missing or incompatible"),
        (
            {
                "bad": {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": False,
                }
            },
            "missing or incompatible",
        ),
        (
            {
                "wrong-order": {
                    "key": list(reversed(mongodb_module._RECORD_INDEX)),
                    "unique": True,
                }
            },
            "missing or incompatible",
        ),
        (
            {
                "collated": {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": True,
                    "collation": {"locale": "en"},
                }
            },
            "missing or incompatible",
        ),
        (
            {
                "sparse": {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": True,
                    "sparse": True,
                }
            },
            "missing or incompatible",
        ),
        (
            {
                "partial": {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": True,
                    "partialFilterExpression": {"bucket": "nodes"},
                }
            },
            "missing or incompatible",
        ),
    ],
)
def test_mongodb_rejects_missing_or_incompatible_record_index(
    details: dict[str, dict[str, object]],
    message: str,
) -> None:
    store = object.__new__(mongodb_module.MongoStore)
    store._records = SimpleNamespace(index_information=lambda: details)
    with pytest.raises(IntegrityError, match=message):
        store._require_record_index()


def test_mongodb_fresh_index_creation_is_explicit_and_validated() -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    class Records:
        def __init__(self) -> None:
            self.created = False

        def index_information(self) -> dict[str, dict[str, object]]:
            if not self.created:
                return {"_id_": {"key": [("_id", 1)]}}
            return {
                "_id_": {"key": [("_id", 1)]},
                mongodb_module._RECORD_INDEX_NAME: {
                    "key": list(mongodb_module._RECORD_INDEX),
                    "unique": True,
                },
            }

        def create_index(
            self,
            keys: object,
            **options: object,
        ) -> None:
            calls.append((keys, options))
            self.created = True

    store = object.__new__(mongodb_module.MongoStore)
    store._records = Records()
    store._ensure_record_index_for_fresh_namespace()
    store._require_record_index()
    assert calls == [
        (
            mongodb_module._RECORD_INDEX,
            {
                "unique": True,
                "name": mongodb_module._RECORD_INDEX_NAME,
            },
        )
    ]


def test_mongodb_fresh_identity_schema_and_revision_initialize_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Session:
        def __enter__(self) -> Session:
            events.append("session-enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("session-exit")

        def with_transaction(
            self,
            callback: Callable[[Session], object],
            **options: object,
        ) -> object:
            events.append(("transaction", options))
            return callback(self)

    class Coordinators:
        def find_one_and_update(
            self,
            query: dict[str, object],
            update: object,
            **options: object,
        ) -> dict[str, object]:
            events.append(("coordinator", query, update, options))
            return {
                "_id": "store",
                "store_id": "store",
                "revision": 1,
                "locked_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }

    class Records:
        def find_one(self, *_args: object, **_kwargs: object) -> None:
            return None

        def replace_one(
            self,
            query: dict[str, object],
            document: dict[str, object],
            **options: object,
        ) -> None:
            events.append(("schema", query, document, options))

    store = object.__new__(mongodb_module.MongoStore)
    store.store_id = "store"
    store._client = SimpleNamespace(start_session=Session)
    store._records = Records()
    store._coordinators = Coordinators()
    store._pymongo = SimpleNamespace(
        ReturnDocument=SimpleNamespace(AFTER="after")
    )
    monkeypatch.setattr(store, "_namespace_state", lambda _session: "fresh")
    monkeypatch.setattr(
        store,
        "_transaction_options",
        lambda: ("read", "write", "primary"),
    )

    store._initialize_fresh_namespace()

    assert events[0] == "session-enter"
    assert events[1] == (
        "transaction",
        {
            "read_concern": "read",
            "write_concern": "write",
            "read_preference": "primary",
        },
    )
    coordinator_event = events[2]
    assert isinstance(coordinator_event, tuple)
    assert coordinator_event[0] == "coordinator"
    assert coordinator_event[1] == {"_id": "store"}
    assert coordinator_event[3] == {
        "upsert": True,
        "return_document": "after",
        "session": coordinator_event[3]["session"],
    }
    schema_event = events[3]
    assert isinstance(schema_event, tuple)
    assert schema_event[0] == "schema"
    assert schema_event[2]["store_id"] == "store"
    assert schema_event[2]["bucket"] == "schema"
    assert schema_event[2]["key"] == "version"
    assert schema_event[2]["value"] == "1"
    assert events[4] == "session-exit"


def test_mongodb_existing_create_path_performs_validation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = object.__new__(mongodb_module.MongoStore)
    monkeypatch.setattr(
        store,
        "_namespace_state_read",
        lambda: events.append("namespace") or "existing",
    )
    monkeypatch.setattr(
        store,
        "_require_record_index",
        lambda: events.append("index"),
    )
    monkeypatch.setattr(
        store,
        "_ensure_record_index_for_fresh_namespace",
        lambda: events.append("create-index"),
    )
    monkeypatch.setattr(
        store,
        "_initialize_fresh_namespace",
        lambda: events.append("initialize"),
    )

    store._initialize_or_require_store()
    assert events == ["namespace", "index"]


def test_mongodb_write_does_not_recreate_missing_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[object] = []

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def with_transaction(
            self,
            callback: Callable[[Session], object],
            **_options: object,
        ) -> object:
            return callback(self)

    class Coordinators:
        def find_one(self, *_args: object, **_kwargs: object) -> None:
            return None

        def find_one_and_update(
            self,
            *args: object,
            **kwargs: object,
        ) -> None:
            updates.append((args, kwargs))
            return None

    store = object.__new__(mongodb_module.MongoStore)
    store.store_id = "store"
    store._client = SimpleNamespace(start_session=Session)
    store._coordinators = Coordinators()
    store._records = object()
    store._pymongo = SimpleNamespace(
        ReturnDocument=SimpleNamespace(AFTER="after")
    )
    monkeypatch.setattr(
        store,
        "_transaction_options",
        lambda: ("read", "write", "primary"),
    )

    with pytest.raises(IntegrityError, match="coordinator is missing or corrupt"):
        store._write(lambda _tx: None)
    assert updates == []


def test_mongodb_create_failure_closes_client_without_connect_time_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _install_read_only_mongodb(monkeypatch, [None])

    def fail_initialization(_self: object) -> NoReturn:
        raise IntegrityError("initialization failed")

    monkeypatch.setattr(
        mongodb_module.MongoStore,
        "_initialize_or_require_store",
        fail_initialization,
    )
    with pytest.raises(IntegrityError, match="initialization failed"):
        mongodb_module.MongoStore("mongodb://unused", create=True)

    assert len(clients) == 1
    assert clients[0].closed
    assert not clients[0].records.index_calls
    assert not clients[0].records.write_calls
    assert not clients[0].coordinators.write_calls


def test_mongodb_transaction_fails_closed_on_corrupt_records_and_time() -> None:
    record_id = mongodb_module._record_id("store", "bucket", "key")
    valid = {
        "_id": record_id,
        "store_id": "store",
        "bucket": "bucket",
        "key": "key",
        "value": "value",
    }

    class Cursor(list[dict[str, object]]):
        def sort(self, *_args: object) -> Cursor:
            return self

    class Records:
        database = SimpleNamespace(
            command=lambda *_args, **_kwargs: {
                "localTime": datetime(2026, 1, 1, tzinfo=timezone.utc)
            }
        )

        def __init__(self, record: dict[str, object] | None) -> None:
            self.record = record
            self.aggregate_result: list[dict[str, object]] = []

        def find_one(self, *_args: object, **_kwargs: object) -> object:
            return self.record

        def find(self, *_args: object, **_kwargs: object) -> Cursor:
            return Cursor([] if self.record is None else [self.record])

        def aggregate(
            self, *_args: object, **_kwargs: object
        ) -> list[dict[str, object]]:
            return self.aggregate_result

    records = Records({**valid, "value": 1})
    tx = mongodb_module._MongoTransaction(
        records, object(), "store", timestamp=None
    )
    with pytest.raises(IntegrityError, match="value must be a string"):
        tx.get("bucket", "key")

    records.record = {**valid, "key": 1}
    with pytest.raises(IntegrityError, match="invalid MongoDB Pollard record"):
        tx.items("bucket")

    records.record = {**valid, "store_id": "other"}
    with pytest.raises(IntegrityError, match="collision or corruption"):
        tx.get("bucket", "key")

    records.record = None
    assert tx.now() == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    assert tx.now() == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()

    invalid_time = Records(None)
    invalid_time.database = SimpleNamespace(
        command=lambda *_args, **_kwargs: {"localTime": None}
    )
    with pytest.raises(IntegrityError, match="current time"):
        mongodb_module._MongoTransaction(
            invalid_time, object(), "store", timestamp=None
        ).now()


def test_redis_transaction_fails_closed_on_corrupt_values_and_types() -> None:
    class Pipe:
        value: object = None
        bucket: object = {}

        def hget(self, *_args: object) -> object:
            return self.value

        def hgetall(self, *_args: object) -> object:
            return self.bucket

    pipe = Pipe()
    tx = redis_module._RedisTransaction(pipe, lambda bucket: bucket, 1.5)
    pipe.value = b"bytes"
    with pytest.raises(IntegrityError, match="stored value"):
        tx.get("bucket", "key")
    pipe.bucket = []
    with pytest.raises(IntegrityError, match="must be a hash"):
        tx.items("bucket")
    pipe.bucket = {1: "value"}
    with pytest.raises(IntegrityError, match="entries must be strings"):
        tx.items("bucket")
    with pytest.raises(TypeError, match="keys and values"):
        tx.put("bucket", "key", 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="keys must be strings"):
        tx.delete("bucket", 1)  # type: ignore[arg-type]

    pipe.bucket = {"keep": "old", "drop": "old"}
    tx.put("bucket", "keep", "new")
    tx.delete("bucket", "drop")
    tx.put("other", "ignored", "value")
    assert tx.get("bucket", "keep") == "new"
    assert tx.items("bucket") == [("keep", "new")]

    writes: list[tuple[str, str, str | None]] = []
    destination = SimpleNamespace(
        hset=lambda bucket, key, value: writes.append((bucket, key, value)),
        hdel=lambda bucket, key: writes.append((bucket, key, None)),
    )
    tx.queue_writes(destination)
    assert ("bucket", "keep", "new") in writes
    assert ("bucket", "drop", None) in writes


def _install_read_only_neo4j(
    monkeypatch: pytest.MonkeyPatch,
    schema_versions: list[str | None],
) -> list[Any]:
    drivers: list[Any] = []

    class Result:
        def __init__(
            self,
            record: dict[str, object] | None,
            rows: list[dict[str, object]] | None = None,
        ) -> None:
            self.record = record
            self.rows = rows or []

        def single(self) -> dict[str, object] | None:
            return self.record

        def consume(self) -> None:
            return None

        def data(self) -> list[dict[str, object]]:
            return self.rows

    class Transaction:
        def __init__(self, driver: Driver) -> None:
            self.driver = driver

        def run(self, query: str, **parameters: object) -> Result:
            normalized = " ".join(query.split())
            self.driver.queries.append((normalized, dict(parameters)))
            if normalized.startswith("SHOW CONSTRAINTS"):
                return Result(
                    None,
                    [
                        {
                            "name": name,
                            "type": "UNIQUENESS",
                            "entityType": "NODE",
                            "labelsOrTypes": [label],
                            "properties": [property_name],
                            "ownedIndex": name,
                        }
                        for name, (label, property_name) in (
                            neo4j_module._EXPECTED_CONSTRAINTS.items()
                        )
                    ],
                )
            if normalized.startswith("SHOW INDEXES"):
                return Result(
                    None,
                    [
                        {
                            "name": name,
                            "state": "ONLINE",
                            "type": "RANGE",
                            "entityType": "NODE",
                            "labelsOrTypes": [label],
                            "properties": [property_name],
                            "owningConstraint": name,
                        }
                        for name, (label, property_name) in (
                            neo4j_module._EXPECTED_CONSTRAINTS.items()
                        )
                    ],
                )
            if "properties(coordinator) AS properties" in normalized:
                if self.driver.schema_version is None:
                    return Result(None)
                return Result(
                    {
                        "properties": {
                            "coordinator_key": neo4j_module._coordinator_key(
                                "default"
                            ),
                            "store_id": "default",
                            "revision": 1,
                        }
                    }
                )
            if "RETURN record.record_key AS record_key" in normalized:
                return Result(None)
            if "properties(record) AS properties" not in normalized:
                return Result(None)

            record_key = neo4j_module._record_key("default", "schema", "version")
            assert parameters == {"record_key": record_key}
            if self.driver.schema_version is None:
                return Result(None)
            return Result(
                {
                    "properties": {
                        "record_key": record_key,
                        "store_id": "default",
                        "bucket": "schema",
                        "item_key": "version",
                        "value": self.driver.schema_version,
                    }
                }
            )

    class Session:
        def __init__(self, driver: Driver) -> None:
            self.driver = driver

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_write(self, callback: Callable[[Transaction], object]) -> object:
            self.driver.execute_write_calls += 1
            return callback(Transaction(self.driver))

    class Driver:
        def __init__(self, schema_version: str | None) -> None:
            self.schema_version = schema_version
            self.closed = False
            self.verify_calls = 0
            self.execute_write_calls = 0
            self.session_configs: list[dict[str, object]] = []
            self.queries: list[tuple[str, dict[str, object]]] = []

        def verify_connectivity(self) -> None:
            self.verify_calls += 1

        def session(self, **config: object) -> Session:
            self.session_configs.append(dict(config))
            return Session(self)

        def close(self) -> None:
            self.closed = True

    def connect(
        uri: str,
        *,
        auth: object,
        **driver_config: object,
    ) -> Driver:
        assert uri == "bolt://unused"
        assert auth == ("user", "password")
        assert not driver_config
        driver = Driver(schema_versions[len(drivers)])
        drivers.append(driver)
        return driver

    fake_neo4j = SimpleNamespace(
        GraphDatabase=SimpleNamespace(driver=connect),
        WRITE_ACCESS="WRITE",
        exceptions=SimpleNamespace(),
    )
    monkeypatch.setattr(neo4j_module, "import_module", lambda _name: fake_neo4j)
    return drivers


def _assert_neo4j_schema_read_only(driver: Any) -> None:
    assert driver.verify_calls == 1
    expected_calls = 3 if driver.schema_version == "1" else 1
    assert driver.execute_write_calls == expected_calls
    assert driver.session_configs == [
        {
            "database": "neo4j",
            "default_access_mode": "WRITE",
        }
    ] * expected_calls
    assert driver.queries
    for query, _parameters in driver.queries:
        upper_query = query.upper()
        assert "CREATE CONSTRAINT" not in upper_query
        assert "MERGE " not in upper_query
        assert " SET " not in upper_query
        assert " DELETE " not in upper_query
    schema_query, parameters = driver.queries[0]
    assert schema_query.startswith("MATCH (record:_PollardKV")
    assert "properties(record) AS properties" in schema_query
    assert parameters == {
        "record_key": neo4j_module._record_key("default", "schema", "version")
    }
    if driver.schema_version == "1":
        assert any(query.startswith("SHOW CONSTRAINTS") for query, _ in driver.queries)
        assert any(query.startswith("SHOW INDEXES") for query, _ in driver.queries)


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        ("1", None),
        (None, "schema version: missing"),
        ("999", "schema version: 999"),
    ],
)
def test_neo4j_create_false_requires_existing_schema_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str | None,
    message: str | None,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, [schema_version])

    if message is None:
        with neo4j_module.Neo4jStore(
            "bolt://unused",
            ("user", "password"),
            create=False,
        ) as store:
            assert store.create is False
    else:
        with pytest.raises(IntegrityError, match=message):
            neo4j_module.Neo4jStore(
                "bolt://unused",
                ("user", "password"),
                create=False,
            )

    assert len(drivers) == 1
    _assert_neo4j_schema_read_only(drivers[0])
    assert drivers[0].closed


def test_neo4j_reconnect_preserves_create_false_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, ["1", "1"])

    store = neo4j_module.Neo4jStore(
        "bolt://unused",
        ("user", "password"),
        create=False,
    )
    previous = drivers[0]
    store.reconnect()
    replacement = drivers[1]

    assert store.create is False
    assert store._driver is replacement
    assert previous.closed
    assert not replacement.closed
    for driver in drivers:
        _assert_neo4j_schema_read_only(driver)

    store.close()
    assert replacement.closed


def test_neo4j_reconnect_after_create_mode_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, ["1", "1"])
    store = neo4j_module.Neo4jStore(
        "bolt://unused",
        ("user", "password"),
        create=False,
    )
    store.create = True

    store.reconnect()

    assert store.create is True
    assert store._driver is drivers[1]
    assert drivers[0].closed
    for driver in drivers:
        _assert_neo4j_schema_read_only(driver)
    store.close()


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        (None, "schema version: missing"),
        ("999", "schema version: 999"),
    ],
)
def test_neo4j_create_false_reconnect_keeps_previous_driver_on_invalid_schema(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str | None,
    message: str,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, ["1", schema_version])
    store = neo4j_module.Neo4jStore(
        "bolt://unused",
        ("user", "password"),
        create=False,
    )
    previous = drivers[0]

    with pytest.raises(IntegrityError, match=message):
        store.reconnect()

    replacement = drivers[1]
    assert store.create is False
    assert store._driver is previous
    assert not previous.closed
    assert replacement.closed
    for driver in drivers:
        _assert_neo4j_schema_read_only(driver)

    store.close()
    assert previous.closed


def test_neo4j_initial_schema_read_avoids_missing_property_notifications() -> None:
    class Result:
        def single(self) -> dict[str, object]:
            return {
                "properties": {
                    "record_key": neo4j_module._record_key(
                        "store", "schema", "version"
                    ),
                    "store_id": "store",
                    "bucket": "schema",
                    "item_key": "version",
                    "value": "1",
                }
            }

    class Transaction:
        query = ""

        def run(self, query: str, **_parameters: object) -> Result:
            self.query = query
            return Result()

    transaction = Transaction()
    kv = neo4j_module._Neo4jKVTransaction(transaction, "store")
    assert kv.get("schema", "version") == "1"
    assert "properties(record) AS properties" in transaction.query
    assert "record.bucket AS bucket" not in transaction.query


@pytest.mark.parametrize(
    ("state", "expected", "message"),
    [
        ("fresh", "fresh", None),
        ("valid", "existing", None),
        ("schema-only", None, "initialization state is partial"),
        ("coordinator-only", None, "initialization state is partial"),
        ("data-only", None, "initialization state is partial"),
        ("future-schema", None, "schema version: 999"),
        ("bad-revision", None, "coordinator key collision or corruption"),
        ("bad-identity", None, "coordinator key collision or corruption"),
    ],
)
def test_neo4j_namespace_states_fail_closed(
    state: str,
    expected: str | None,
    message: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    kv = neo4j_module._Neo4jKVTransaction(object(), "store")
    version = (
        "999"
        if state == "future-schema"
        else (
            "1"
            if state
            in {
                "valid",
                "schema-only",
                "bad-revision",
                "bad-identity",
            }
            else None
        )
    )
    coordinator = (
        {
            "coordinator_key": neo4j_module._coordinator_key("store"),
            "store_id": "wrong" if state == "bad-identity" else "store",
            "revision": 0 if state == "bad-revision" else 1,
        }
        if state
        in {
            "valid",
            "coordinator-only",
            "future-schema",
            "bad-revision",
            "bad-identity",
        }
        else None
    )
    monkeypatch.setattr(kv, "get", lambda _bucket, _key: version)
    monkeypatch.setattr(kv, "coordinator", lambda: coordinator)
    monkeypatch.setattr(kv, "has_records", lambda: state == "data-only")

    if message is None:
        assert store._namespace_state(kv) == expected
    else:
        with pytest.raises(IntegrityError, match=message):
            store._namespace_state(kv)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("existing", ["classify", "require"]),
        ("fresh", ["classify", "ensure", "initialize", "require"]),
    ],
)
def test_neo4j_create_mode_orders_validation_and_initialization(
    state: str,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    store._driver = object()
    events: list[str] = []
    monkeypatch.setattr(
        store,
        "_namespace_state_read",
        lambda _driver: events.append("classify") or state,
    )
    monkeypatch.setattr(
        store,
        "_ensure_constraints",
        lambda _driver: events.append("ensure"),
    )
    monkeypatch.setattr(
        store,
        "_initialize_fresh_namespace",
        lambda: events.append("initialize"),
    )
    monkeypatch.setattr(
        store,
        "_require_constraints",
        lambda _driver: events.append("require"),
    )

    store._initialize_or_require_store()

    assert events == expected


def test_neo4j_create_mode_stops_before_ddl_for_partial_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    store._driver = object()

    def partial(_driver: object) -> str:
        raise IntegrityError("Neo4j store initialization state is partial")

    monkeypatch.setattr(store, "_namespace_state_read", partial)
    monkeypatch.setattr(
        store,
        "_ensure_constraints",
        lambda _driver: pytest.fail("partial namespace reached constraint DDL"),
    )
    monkeypatch.setattr(
        store,
        "_initialize_fresh_namespace",
        lambda: pytest.fail("partial namespace reached logical initialization"),
    )

    with pytest.raises(IntegrityError, match="initialization state is partial"):
        store._initialize_or_require_store()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "name": "pollard_neo4j_kv_record_key",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["_PollardKV"],
                "properties": ["wrong"],
            }
        ],
        [
            {
                "name": "pollard_neo4j_kv_record_key",
                "type": "RANGE",
                "entityType": "NODE",
                "labelsOrTypes": ["_PollardKV"],
                "properties": ["record_key"],
            },
            {
                "name": "pollard_neo4j_coordinator_key",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["_PollardCoordinator"],
                "properties": ["coordinator_key"],
            },
        ],
    ],
)
def test_neo4j_rejects_missing_or_incompatible_constraints(
    rows: list[dict[str, object]],
) -> None:
    class Result:
        def data(self) -> list[dict[str, object]]:
            return rows

    class Transaction:
        def run(self, _query: str) -> Result:
            return Result()

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_write(
            self,
            callback: Callable[[Transaction], object],
        ) -> object:
            return callback(Transaction())

    driver = SimpleNamespace(
        session=lambda **_config: Session(),
    )
    store = object.__new__(neo4j_module.Neo4jStore)
    store.database = "neo4j"
    store._neo4j = SimpleNamespace(WRITE_ACCESS="WRITE")

    with pytest.raises(IntegrityError, match="constraints are missing"):
        store._require_constraints(driver)


def test_neo4j_constraint_state_rejects_partial_and_alternate_names() -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    first = {
        "name": "pollard_neo4j_kv_record_key",
        "type": "UNIQUENESS",
        "entityType": "NODE",
        "labelsOrTypes": ["_PollardKV"],
        "properties": ["record_key"],
        "ownedIndex": "pollard_neo4j_kv_record_key",
    }
    second = {
        "name": "pollard_neo4j_coordinator_key",
        "type": "UNIQUENESS",
        "entityType": "NODE",
        "labelsOrTypes": ["_PollardCoordinator"],
        "properties": ["coordinator_key"],
        "ownedIndex": "pollard_neo4j_coordinator_key",
    }
    first_index = {
        "name": "pollard_neo4j_kv_record_key",
        "state": "ONLINE",
        "type": "RANGE",
        "entityType": "NODE",
        "labelsOrTypes": ["_PollardKV"],
        "properties": ["record_key"],
        "owningConstraint": "pollard_neo4j_kv_record_key",
    }
    second_index = {
        "name": "pollard_neo4j_coordinator_key",
        "state": "ONLINE",
        "type": "RANGE",
        "entityType": "NODE",
        "labelsOrTypes": ["_PollardCoordinator"],
        "properties": ["coordinator_key"],
        "owningConstraint": "pollard_neo4j_coordinator_key",
    }

    assert store._constraint_state([], []) == "fresh"
    assert store._constraint_state([first], [first_index]) == "partial"
    assert (
        store._constraint_state(
            [first, second],
            [first_index, second_index],
        )
        == "existing"
    )
    assert (
        store._constraint_state(
            [{**first, "name": "operator_constraint"}],
            [],
        )
        == "partial"
    )
    assert (
        store._constraint_state(
            [],
            [{**first_index, "name": "operator_index"}],
        )
        == "partial"
    )
    assert (
        store._constraint_state(
            [first, second],
            [{**first_index, "state": "POPULATING"}, second_index],
        )
        == "partial"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"state": "FAILED"},
        {"type": "TEXT"},
        {"entityType": "RELATIONSHIP"},
        {"labelsOrTypes": ["Wrong"]},
        {"properties": ["wrong"]},
        {"owningConstraint": "wrong"},
    ],
)
def test_neo4j_constraint_state_rejects_incompatible_backing_index(
    change: dict[str, object],
) -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    constraints = [
        {
            "name": name,
            "type": "UNIQUENESS",
            "entityType": "NODE",
            "labelsOrTypes": [label],
            "properties": [property_name],
            "ownedIndex": name,
        }
        for name, (label, property_name) in (
            neo4j_module._EXPECTED_CONSTRAINTS.items()
        )
    ]
    indexes = [
        {
            "name": name,
            "state": "ONLINE",
            "type": "RANGE",
            "entityType": "NODE",
            "labelsOrTypes": [label],
            "properties": [property_name],
            "owningConstraint": name,
        }
        for name, (label, property_name) in (
            neo4j_module._EXPECTED_CONSTRAINTS.items()
        )
    ]
    indexes[0] = {**indexes[0], **change}

    assert store._constraint_state(constraints, indexes) == "partial"


def test_neo4j_creates_both_constraints_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    execute_write_calls = 0

    class Result:
        def consume(self) -> None:
            return None

    class Transaction:
        def run(self, query: str) -> Result:
            queries.append(" ".join(query.split()))
            return Result()

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_write(
            self,
            callback: Callable[[Transaction], object],
        ) -> object:
            nonlocal execute_write_calls
            execute_write_calls += 1
            return callback(Transaction())

    store = object.__new__(neo4j_module.Neo4jStore)
    store.database = "neo4j"
    store._neo4j = SimpleNamespace(WRITE_ACCESS="WRITE")
    driver = SimpleNamespace(session=lambda **_config: Session())
    monkeypatch.setattr(store, "_constraint_rows", lambda _driver: [])
    monkeypatch.setattr(store, "_index_rows", lambda _driver: [])
    monkeypatch.setattr(store, "_require_constraints", lambda _driver: None)

    store._ensure_constraints(driver)

    assert execute_write_calls == 1
    assert len(queries) == 2
    assert all(query.startswith("CREATE CONSTRAINT") for query in queries)


def test_neo4j_does_not_repair_partial_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object.__new__(neo4j_module.Neo4jStore)
    first = {
        "name": "pollard_neo4j_kv_record_key",
        "type": "UNIQUENESS",
        "entityType": "NODE",
        "labelsOrTypes": ["_PollardKV"],
        "properties": ["record_key"],
    }
    monkeypatch.setattr(store, "_constraint_rows", lambda _driver: [first])
    monkeypatch.setattr(store, "_index_rows", lambda _driver: [])
    driver = SimpleNamespace(
        session=lambda **_config: pytest.fail(
            "partial constraint state must not be repaired"
        )
    )

    with pytest.raises(IntegrityError, match="constraints are missing"):
        store._ensure_constraints(driver)


def test_neo4j_write_does_not_recreate_missing_coordinator() -> None:
    queries: list[str] = []

    class Result:
        def single(self) -> None:
            return None

    class Transaction:
        def run(self, query: str, **_parameters: object) -> Result:
            queries.append(" ".join(query.split()))
            return Result()

    kv = neo4j_module._Neo4jKVTransaction(Transaction(), "store")
    with pytest.raises(IntegrityError, match="coordinator is missing"):
        kv.lock()
    assert len(queries) == 1
    assert "MATCH (coordinator:_PollardCoordinator" in queries[0]
    assert "MERGE " not in queries[0]


def test_neo4j_fresh_schema_and_coordinator_initialize_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    execute_calls = 0

    class Result:
        def __init__(self, record: dict[str, object]) -> None:
            self.record = record

        def single(self) -> dict[str, object]:
            return self.record

    class Transaction:
        def run(self, query: str, **parameters: object) -> Result:
            normalized = " ".join(query.split())
            queries.append(normalized)
            if "MERGE (coordinator:_PollardCoordinator" in normalized:
                return Result(
                    {
                        "properties": {
                            "coordinator_key": neo4j_module._coordinator_key(
                                "store"
                            ),
                            "store_id": "store",
                            "revision": 1,
                        }
                    }
                )
            record_key = neo4j_module._record_key(
                "store", "schema", "version"
            )
            if normalized.startswith("MERGE (record:_PollardKV"):
                return Result(
                    {
                        "record_key": record_key,
                        "store_id": "store",
                        "bucket": "schema",
                        "item_key": "version",
                        "value": "1",
                    }
                )
            assert normalized.startswith("MATCH (record:_PollardKV")
            assert parameters["record_key"] == record_key
            return Result({"record_key": record_key})

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_write(
            self,
            callback: Callable[[Transaction], object],
        ) -> object:
            nonlocal execute_calls
            execute_calls += 1
            return callback(Transaction())

    store = object.__new__(neo4j_module.Neo4jStore)
    store.database = "neo4j"
    store.store_id = "store"
    store._driver = SimpleNamespace(session=lambda **_config: Session())
    store._neo4j = SimpleNamespace(WRITE_ACCESS="WRITE")
    monkeypatch.setattr(store, "_namespace_state", lambda _tx: "fresh")

    store._initialize_fresh_namespace()

    assert execute_calls == 1
    assert any(
        "MERGE (coordinator:_PollardCoordinator" in query
        for query in queries
    )
    assert any("MERGE (record:_PollardKV" in query for query in queries)


def test_neo4j_create_failure_closes_driver_without_constraint_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, [None])

    def fail_initialization(_self: object) -> NoReturn:
        raise IntegrityError("initialization failed")

    monkeypatch.setattr(
        neo4j_module.Neo4jStore,
        "_initialize_or_require_store",
        fail_initialization,
    )
    with pytest.raises(IntegrityError, match="initialization failed"):
        neo4j_module.Neo4jStore(
            "bolt://unused",
            ("user", "password"),
            create=True,
        )

    assert len(drivers) == 1
    assert drivers[0].closed
    assert drivers[0].queries == []


def test_neo4j_logical_initialization_failure_closes_driver_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = _install_read_only_neo4j(monkeypatch, [None])
    events: list[str] = []

    def constraints_ready(_self: object, _driver: object) -> None:
        events.append("constraints")

    def fail_logical_initialization(_self: object) -> NoReturn:
        events.append("logical")
        raise IntegrityError("logical initialization rolled back")

    monkeypatch.setattr(
        neo4j_module.Neo4jStore,
        "_ensure_constraints",
        constraints_ready,
    )
    monkeypatch.setattr(
        neo4j_module.Neo4jStore,
        "_initialize_fresh_namespace",
        fail_logical_initialization,
    )

    with pytest.raises(IntegrityError, match="logical initialization rolled back"):
        neo4j_module.Neo4jStore(
            "bolt://unused",
            ("user", "password"),
            create=True,
        )

    assert events == ["constraints", "logical"]
    assert drivers[0].closed
    for query, _parameters in drivers[0].queries:
        upper_query = query.upper()
        assert "CREATE CONSTRAINT" not in upper_query
        assert "MERGE " not in upper_query
        assert " SET " not in upper_query
        assert " DELETE " not in upper_query


def test_transactional_store_refuses_corrupt_schema_and_nodes() -> None:
    store = _FakeStore()
    store.data["schema"]["version"] = "999"
    with pytest.raises(IntegrityError, match="schema version: 999"):
        store._require_transactional_store()

    store.data["schema"]["version"] = "1"
    store.data.setdefault("nodes", {})["bad"] = "[]"
    with pytest.raises(IntegrityError, match="must be an object"):
        store.get("bad")
    store.data["nodes"]["bad"] = json.dumps(
        {
            "id": 1,
            "parent": None,
            "kind": "root",
            "attempt": 0,
            "payload": "{}",
            "result": None,
            "result_digest": None,
            "meta": "{}",
        }
    )
    with pytest.raises(IntegrityError, match="stored id"):
        store.get("bad")


def test_transactional_store_refuses_corrupt_reservation_and_window_state() -> None:
    store = _FakeStore()
    assert store._pollard_reserve("active", [_budget()], [], 60).ok
    active = json.loads(store.data["reservations"]["active"])
    active["expires_at"] = "not-a-number"
    store.data["reservations"]["active"] = json.dumps(active)
    with pytest.raises(IntegrityError, match="expires_at"):
        store._pollard_reserve("active", [_budget()], [], 60)

    active["expires_at"] = store.clock + 60
    active["details"] = "wrong"
    store.data["reservations"]["active"] = json.dumps(active)
    with pytest.raises(IntegrityError, match="reservation details"):
        store._pollard_reserve("other", [_budget()], [], 60)

    window = WindowReservation(
        ledger_key="window",
        meter="requests",
        limit=Decimal("1"),
        amount=Decimal("1"),
        window_seconds=60,
    )
    store.data["reservations"]["active"] = json.dumps(
        {**active, "state": "released", "details": []}
    )
    store.data.setdefault("window-events", {})["bad"] = "{}"
    with pytest.raises(IntegrityError, match="settled_at"):
        store._pollard_reserve("window", [], [window], 60)


def test_transactional_store_refuses_corrupt_settlement_details() -> None:
    mutations: list[tuple[Callable[[dict[str, Any]], None], str]] = [
        (lambda state: state.update(details=[]), "details are missing"),
        (lambda state: state.update(details=[1]), "invalid reservation details"),
        (
            lambda state: state.update(
                details=[{"kind": "unknown", "scope_id": "s", "meter": "steps"}]
            ),
            "invalid reservation kind",
        ),
    ]
    for mutation, message in mutations:
        store = _FakeStore()
        assert store._pollard_reserve("settle", [_budget()], [], 60).ok
        state = json.loads(store.data["reservations"]["settle"])
        mutation(state)
        store.data["reservations"]["settle"] = json.dumps(state)
        with pytest.raises(IntegrityError, match=message):
            store._pollard_settle("settle", {"steps": Decimal("1")})

    missing = _FakeStore()
    assert missing._pollard_reserve("settle", [_budget()], [], 60).ok
    missing.data["budget"].pop(_compound_key("scope", "steps"))
    with pytest.raises(IntegrityError, match="budget state missing"):
        missing._pollard_settle("settle", {"steps": Decimal("1")})


def test_transactional_release_connection_failure_is_explicitly_uncertain() -> None:
    store = _FakeStore()
    assert store._pollard_reserve("release", [_budget()], [], 60).ok
    store.failures = ["before", "before"]
    with pytest.raises(ReservationUncertain) as error:
        store._pollard_release("release")
    assert error.value.reservation_id == "release"
    assert isinstance(error.value.__cause__, _ConnectionLost)


def test_kafka_codec_and_state_machine_failure_paths() -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "root"})
    body = kafka_module._node_record(root)
    event, operation_id = kafka_module._event("store", "put", body)

    with pytest.raises(ValueError, match="unsupported"):
        kafka_module._event("store", "delete", {})
    with pytest.raises(IntegrityError, match="envelope fields"):
        kafka_module._parse_event(b"{}", offset=1, store_id="store")

    changed = dict(event)
    changed["version"] = 999
    with pytest.raises(IntegrityError, match="unknown event version"):
        kafka_module._parse_event(
            kafka_module._json_bytes(changed), offset=2, store_id="store"
        )
    changed = dict(event)
    changed["operation"] = "delete"
    with pytest.raises(IntegrityError, match="invalid operation"):
        kafka_module._parse_event(
            kafka_module._json_bytes(changed), offset=3, store_id="store"
        )
    with pytest.raises(IntegrityError, match="canonically encoded"):
        kafka_module._parse_event(
            json.dumps(event, indent=2).encode(), offset=4, store_id="store"
        )

    store = object.__new__(KafkaStore)
    store._nodes = {}
    store._children = {}
    store._outcomes = {}
    child = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"child": True})
    store._apply_put("missing-parent", child)
    with pytest.raises(KeyError, match=root.id):
        store._raise_outcome("missing-parent")
    store._apply_put(operation_id, root)
    store._raise_outcome(operation_id)
    store._apply_meta("missing-node", {"id": "missing", "patch": {}}, offset=5)
    with pytest.raises(KeyError, match="missing"):
        store._raise_outcome("missing-node")
    with pytest.raises(IntegrityError, match="invalid meta fields"):
        store._apply_meta("bad", {}, offset=6)
    with pytest.raises(IntegrityError, match="invalid meta patch"):
        store._apply_meta("bad", {"id": 1, "patch": {}}, offset=7)
    with pytest.raises(IntegrityError, match="no replay outcome"):
        store._raise_outcome("unknown")
    store._outcomes["strange"] = ("strange", None)
    with pytest.raises(IntegrityError, match="unknown outcome"):
        store._raise_outcome("strange")


def test_kafka_node_decoder_refuses_invalid_records() -> None:
    with pytest.raises(IntegrityError, match="invalid node fields"):
        kafka_module._node_from_record({}, offset=1)
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "decode"})
    record = kafka_module._node_record(root)
    record["attempt"] = True
    with pytest.raises(IntegrityError, match="invalid node"):
        kafka_module._node_from_record(record, offset=2)
    with pytest.raises(ValueError, match="unsupported JSON constant"):
        kafka_module._reject_json_constant("NaN")


def test_kafka_result_conflict_is_deterministic() -> None:
    store = object.__new__(KafkaStore)
    store._nodes = {}
    store._children = {}
    store._outcomes = {}
    first = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "conflict"},
        result={"text": "first"},
    )
    second = replace(
        first,
        result={"text": "second"},
        result_digest=None,
        _result_text=None,
    )
    store._apply_put("first", first)
    store._apply_put("second", second)
    assert store._nodes[first.id].result == {"text": "first"}
    assert store._nodes[first.id].meta["result_conflicts"][0]["result"] == {
        "text": "second"
    }


def _bare_kafka_store() -> KafkaStore:
    store = object.__new__(KafkaStore)
    backend: Any = store
    store.topic = "topic"
    store.store_id = "store"
    store.read_only = False
    store.timeout = 1.0
    store._next_offset = 0
    store._snapshot_high_offset = None
    store._operation_offsets = {}
    store._operations = {}
    store._event_digests = []
    store._nodes = {}
    store._children = {}
    store._outcomes = {}
    backend._kafka = SimpleNamespace(TopicPartition=lambda *_args: object())
    return store


def test_kafka_invalid_command_does_not_poison_same_offset_retry() -> None:
    store = _bare_kafka_store()
    event, operation_id = kafka_module._event(
        "store",
        "meta",
        {"id": "missing-patch"},
    )
    encoded = kafka_module._json_bytes(event)

    for _attempt in range(2):
        with pytest.raises(IntegrityError, match="invalid meta fields"):
            store._apply_message(0, b"store", encoded)
        assert operation_id not in store._operations
        assert operation_id not in store._operation_offsets
        assert operation_id not in store._outcomes


def test_store_package_lazy_exports_and_unknown_attribute() -> None:
    import pollard.stores as stores

    assert stores.RedisStore is redis_module.RedisStore
    assert stores.MongoStore is mongodb_module.MongoStore
    assert stores.Neo4jStore is neo4j_module.Neo4jStore
    assert stores.KafkaStore is kafka_module.KafkaStore
    with pytest.raises(AttributeError, match="no attribute"):
        attrgetter("UnknownStore")(stores)


def test_kafka_produce_recovery_finds_committed_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _bare_kafka_store()
    attempts = 0

    def fail(_value: bytes) -> int:
        nonlocal attempts
        attempts += 1
        raise OSError("lost acknowledgement")

    def sync() -> None:
        store._operation_offsets["operation"] = 7

    monkeypatch.setattr(store, "_produce_once", fail)
    monkeypatch.setattr(store, "_sync_current", sync)
    assert store._produce_with_recovery(value=b"event", operation_id="operation") == 7
    assert attempts == 1


def test_kafka_produce_recovery_reports_persistent_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _bare_kafka_store()
    monkeypatch.setattr(
        store,
        "_produce_once",
        lambda _value: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(store, "_sync_current", lambda: None)
    with pytest.raises(IntegrityError, match="outcome is uncertain") as error:
        store._produce_with_recovery(value=b"event", operation_id="operation")
    assert isinstance(error.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("watermarks", "next_offset", "message"),
    [((1, 1), 0, "history was truncated"), ((0, 1), 2, "behind the replay cursor")],
)
def test_kafka_watermarks_fail_closed(
    watermarks: tuple[int, int], next_offset: int, message: str
) -> None:
    store = _bare_kafka_store()
    store._next_offset = next_offset
    store._consumer = SimpleNamespace(
        get_watermark_offsets=lambda *_args, **_kwargs: watermarks
    )
    with pytest.raises(IntegrityError, match=message):
        store._sync_current()


def test_kafka_watermark_failure_and_forward_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _bare_kafka_store()
    store._consumer = SimpleNamespace(
        get_watermark_offsets=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("offline")
        )
    )
    with pytest.raises(IntegrityError, match="watermarks"):
        store._sync_current()

    store._consumer = SimpleNamespace(
        get_watermark_offsets=lambda *_args, **_kwargs: (0, 3)
    )
    targets: list[int] = []
    monkeypatch.setattr(store, "_sync_to_offset", targets.append)
    store._sync_current()
    assert targets == [2]


class _Message:
    def __init__(
        self,
        *,
        topic: str = "topic",
        partition: int = 0,
        offset: int = 0,
        error: object = None,
        key: object = b"store",
        value: object = b"{}",
    ) -> None:
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._error = error
        self._key = key
        self._value = value

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def error(self) -> object:
        return self._error

    def key(self) -> object:
        return self._key

    def value(self) -> object:
        return self._value


@pytest.mark.parametrize(
    ("message", "match"),
    [
        (_Message(error="broker error"), "returned a broker error"),
        (_Message(topic="other"), "crossed"),
        (_Message(offset=2), "offset gap"),
    ],
)
def test_kafka_replay_message_failures(message: _Message, match: str) -> None:
    store = _bare_kafka_store()
    store._consumer = SimpleNamespace(poll=lambda _timeout: message)
    with pytest.raises(IntegrityError, match=match):
        store._sync_to_offset(0)
    store._next_offset = 1
    store._sync_to_offset(0)


def test_kafka_replay_poll_failure() -> None:
    store = _bare_kafka_store()
    store._consumer = SimpleNamespace(
        poll=lambda _timeout: (_ for _ in ()).throw(OSError("offline"))
    )
    with pytest.raises(IntegrityError, match="replay failed"):
        store._sync_to_offset(0)


def test_kafka_apply_message_rejects_wrong_key_and_nonbytes() -> None:
    store = _bare_kafka_store()
    with pytest.raises(IntegrityError, match="wrong store key"):
        store._apply_message(0, b"other", b"{}")
    with pytest.raises(IntegrityError, match="not a byte record"):
        store._apply_message(0, b"store", "text")


@pytest.mark.parametrize(
    ("message", "match"),
    [
        (_Message(topic="other"), "unexpected location"),
        (_Message(partition=1), "unexpected location"),
        (_Message(offset=-1), "valid offset"),
    ],
)
def test_kafka_delivery_callback_validation(message: _Message, match: str) -> None:
    store = _bare_kafka_store()

    class Producer:
        def produce(self, *_args: object, **kwargs: object) -> None:
            callback = kwargs["on_delivery"]
            callback(None, message)  # type: ignore[operator]

        def poll(self, _timeout: float) -> None:
            return None

    store._producer = Producer()
    with pytest.raises(IntegrityError, match=match):
        store._produce_once(b"event")


def test_kafka_delivery_enqueue_and_missing_message_failures() -> None:
    store = _bare_kafka_store()
    store._producer = SimpleNamespace(
        produce=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full"))
    )
    with pytest.raises(IntegrityError, match="could not be enqueued"):
        store._produce_once(b"event")

    class Producer:
        def produce(self, *_args: object, **kwargs: object) -> None:
            callback = kwargs["on_delivery"]
            callback(None, None)  # type: ignore[operator]

        def poll(self, _timeout: float) -> None:
            return None

    store._producer = Producer()
    with pytest.raises(IntegrityError, match="no message"):
        store._produce_once(b"event")
