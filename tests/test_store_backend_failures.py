from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
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
        (redis_module.RedisStore, ("",), {}, "url"),
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


@pytest.mark.parametrize(
    "value",
    [None, [1], [1, 2, 3], [True, 1], [1, True], [-1, 0], [1, -1], [1, 1_000_000]],
)
def test_redis_server_time_fails_closed(value: object) -> None:
    with pytest.raises(IntegrityError, match="TIME"):
        _server_time(value)


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
    store.timeout = 1.0
    store._next_offset = 0
    store._operation_offsets = {}
    store._operations = {}
    store._nodes = {}
    store._children = {}
    store._outcomes = {}
    backend._kafka = SimpleNamespace(TopicPartition=lambda *_args: object())
    return store


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
        (_Message(error="broker error"), "returned an error"),
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
