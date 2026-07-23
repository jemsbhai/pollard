from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest

from pollard import KafkaStore, gc, seal, verify
from pollard.arbiter import TransactionalArbiter
from pollard.errors import IntegrityError
from pollard.stores.kafka import _event, _parse_event
from pollard.tree import Node, NodeKind


@contextmanager
def _topic(*, partitions: int = 1, valid: bool = True) -> Iterator[str]:
    if not os.environ.get("POLLARD_TEST_KAFKA_BOOTSTRAP"):
        pytest.skip("Kafka is not configured")
    from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]

    bootstrap = os.environ["POLLARD_TEST_KAFKA_BOOTSTRAP"]
    name = f"pollard-kafka-{uuid4().hex}"
    config = {
        "cleanup.policy": "delete",
        "retention.ms": "-1" if valid else "60000",
        "retention.bytes": "-1",
    }
    admin = AdminClient({"bootstrap.servers": bootstrap})
    admin.create_topics([NewTopic(name, partitions, 1, config=config)])[name].result(
        timeout=10
    )
    try:
        yield name
    finally:
        admin.delete_topics([name])[name].result(timeout=10)


def _open(topic: str) -> KafkaStore:
    return KafkaStore(
        {"bootstrap.servers": os.environ["POLLARD_TEST_KAFKA_BOOTSTRAP"]},
        topic=topic,
    )


def test_kafka_is_non_arbiter_and_rebuilds_concurrent_metadata() -> None:
    with _topic() as topic:
        with _open(topic) as initial:
            assert not isinstance(initial, TransactionalArbiter)
            root = Node.make(
                kind=NodeKind.ROOT, parent=None, payload={"run": "kafka"}
            )
            initial.put(root)

        with (
            _open(topic) as first,
            _open(topic) as second,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            list(
                executor.map(
                    lambda pair: pair[0].update_meta(root.id, pair[1]),
                    [(first, {"first": True}), (second, {"second": True})],
                )
            )

        with _open(topic) as reopened:
            assert reopened.get(root.id).meta == {"first": True, "second": True}
            assert verify(reopened, root.id).ok
            assert len(seal(reopened, root.id).entries) == 1
            with pytest.raises(TypeError, match="does not support offline garbage"):
                gc(reopened)


def test_kafka_real_broker_walks_deep_tree_iteratively() -> None:
    with _topic() as topic, _open(topic) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "depth"})
        store.put(root)
        expected = [root.id]
        parent = root
        for index in range(128):
            child = Node.make(
                kind=NodeKind.NOTE,
                parent=parent.id,
                payload={"index": index},
            )
            store.put(child)
            expected.append(child.id)
            parent = child
        assert [node.id for node in store.walk(root.id)] == expected


def test_kafka_failed_preconditions_do_not_poison_later_retry() -> None:
    with _topic() as topic, _open(topic) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "retry"})
        child = Node.make(
            kind=NodeKind.NOTE,
            parent=root.id,
            payload={"retry": True},
        )
        with pytest.raises(KeyError, match=root.id):
            store.put(child)
        store.put(root)
        store.put(child)
        assert store.get(child.id) == child

        later = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "meta-retry"},
        )
        with pytest.raises(KeyError, match=later.id):
            store.update_meta(later.id, {"ready": True})
        store.put(later)
        store.update_meta(later.id, {"ready": True})
        assert store.get(later.id).meta == {"ready": True}


@pytest.mark.parametrize(
    ("partitions", "valid", "message"),
    [(2, True, "exactly partition 0"), (1, False, "retention.ms=-1")],
)
def test_kafka_refuses_incompatible_topic_configuration(
    partitions: int, valid: bool, message: str
) -> None:
    with (
        _topic(partitions=partitions, valid=valid) as topic,
        pytest.raises(IntegrityError, match=message),
    ):
        _open(topic)


def test_kafka_event_codec_is_canonical_and_fails_closed() -> None:
    node = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "codec"})
    body = {
        "id": node.id,
        "parent": None,
        "kind": node.kind,
        "attempt": 0,
        "payload": '{"run":"codec"}',
        "result": None,
        "result_digest": None,
        "meta": "{}",
    }
    event, operation_id = _event("default", "put", body)
    encoded = __import__("json").dumps(
        event, sort_keys=True, separators=(",", ":")
    ).encode()
    assert _parse_event(encoded, offset=0, store_id="default")["operation_id"] == operation_id
    with pytest.raises(IntegrityError, match="not valid JSON"):
        _parse_event(b"not-json", offset=1, store_id="default")
    with pytest.raises(IntegrityError, match="another store"):
        _parse_event(encoded, offset=2, store_id="other")
