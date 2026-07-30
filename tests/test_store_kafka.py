from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import pollard.stores.kafka as kafka_module
from pollard import KafkaStore, SQLiteStore, gc, seal, verify
from pollard.arbiter import TransactionalArbiter
from pollard.cli import main
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


class _FakeKafkaMessage:
    def __init__(
        self,
        offset: int,
        key: bytes,
        value: bytes,
        *,
        topic: str,
    ) -> None:
        self._offset = offset
        self._key = key
        self._value = value
        self._topic = topic

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset

    def error(self) -> None:
        return None

    def key(self) -> bytes:
        return self._key

    def value(self) -> bytes:
        return self._value


class _KafkaHarness:
    def __init__(self, *, topic: str = "audit") -> None:
        self.topic = topic
        self.messages: list[tuple[bytes, bytes]] = []
        self.admin_configs: list[dict[str, object]] = []
        self.consumers: list[Any] = []
        self.producers: list[Any] = []
        self.fail_producer_creation = False
        self.fail_watermark_indices: set[int] = set()
        self.fail_poll_indices: set[int] = set()

    def append_node(self, node: Node, *, store_id: str = "team") -> None:
        event, _operation_id = kafka_module._event(
            store_id,
            "put",
            kafka_module._node_record(node),
        )
        self.messages.append(
            (
                store_id.encode("utf-8"),
                kafka_module._json_bytes(event),
            )
        )


def _install_fake_kafka(
    monkeypatch: pytest.MonkeyPatch,
    *,
    topic: str = "audit",
) -> _KafkaHarness:
    harness = _KafkaHarness(topic=topic)

    class Producer:
        def __init__(self, config: dict[str, object]) -> None:
            if harness.fail_producer_creation:
                raise OSError("producer construction failed")
            self.config = dict(config)
            self.closed = False
            harness.producers.append(self)

        def close(self) -> None:
            self.closed = True

    class Consumer:
        def __init__(self, config: dict[str, object]) -> None:
            self.index = len(harness.consumers)
            self.config = dict(config)
            self.closed = False
            self.cursor = 0
            self.assignments: list[object] = []
            self.watermark_calls = 0
            self.poll_calls = 0
            harness.consumers.append(self)

        def assign(self, partitions: list[object]) -> None:
            self.assignments = list(partitions)

        def get_watermark_offsets(
            self,
            _partition: object,
            **_kwargs: object,
        ) -> tuple[int, int]:
            self.watermark_calls += 1
            if self.index in harness.fail_watermark_indices:
                raise OSError("replacement watermark failure")
            return 0, len(harness.messages)

        def poll(self, _timeout: float) -> _FakeKafkaMessage | None:
            self.poll_calls += 1
            if self.index in harness.fail_poll_indices:
                raise OSError("consumer poll failure")
            if self.cursor >= len(harness.messages):
                return None
            offset = self.cursor
            key, value = harness.messages[offset]
            self.cursor += 1
            return _FakeKafkaMessage(
                offset,
                key,
                value,
                topic=harness.topic,
            )

        def close(self) -> None:
            self.closed = True

    class Future:
        def result(self, *, timeout: float) -> dict[str, str]:
            assert timeout == 30
            return {
                "cleanup.policy": "delete",
                "retention.ms": "-1",
                "retention.bytes": "-1",
            }

    class AdminClient:
        def __init__(self, config: dict[str, object]) -> None:
            harness.admin_configs.append(dict(config))

        def list_topics(self, *, timeout: float) -> object:
            assert timeout == 30
            return SimpleNamespace(
                topics={
                    harness.topic: SimpleNamespace(
                        error=None,
                        partitions={0: object()},
                    )
                }
            )

        def describe_configs(
            self,
            resources: list[object],
            *,
            request_timeout: float,
        ) -> dict[object, Future]:
            assert request_timeout == 30
            assert len(resources) == 1
            return {resources[0]: Future()}

    fake_kafka = SimpleNamespace(
        Producer=Producer,
        Consumer=Consumer,
        TopicPartition=lambda *args: args,
        OFFSET_BEGINNING=-2,
        KafkaException=RuntimeError,
    )
    fake_admin = SimpleNamespace(
        AdminClient=AdminClient,
        ConfigResource=lambda resource_type, name: (resource_type, name),
        ResourceType=SimpleNamespace(TOPIC="topic"),
    )

    def import_fake(name: str) -> object:
        if name == "confluent_kafka":
            return fake_kafka
        if name == "confluent_kafka.admin":
            return fake_admin
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(kafka_module, "import_module", import_fake)
    return harness


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"read_only": 1}, TypeError, "read_only must be a boolean"),
        (
            {"require_existing": 1},
            TypeError,
            "require_existing must be a boolean",
        ),
        ({"timeout": float("nan")}, ValueError, "timeout must be positive"),
        ({"timeout": float("inf")}, ValueError, "timeout must be positive"),
        ({"timeout": float("-inf")}, ValueError, "timeout must be positive"),
    ],
)
def test_kafka_rejects_invalid_read_only_and_nonfinite_timeout(
    kwargs: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            **kwargs,  # type: ignore[arg-type]
        )


def test_kafka_rejects_timeout_too_large_to_normalize() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            timeout=10**10_000,
        )


def test_kafka_controls_client_configuration_and_observer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    client_config = {
        "bootstrap.servers": "unused",
        "allow.auto.create.topics": True,
        "group.id": "caller-controlled",
        "group.instance.id": "static-member",
        "group.protocol": "consumer",
        "enable.metrics.push": True,
    }
    digest = hashlib.sha256(b"audit\0team").hexdigest()
    observer_group = f"pollard-observer-{digest}"
    reader_group = f"pollard-reader-{digest}"

    with (
        KafkaStore(
            client_config,
            topic="audit",
            store_id="team",
            read_only=True,
        ) as first,
        KafkaStore(
            client_config,
            topic="audit",
            store_id="team",
            read_only=True,
        ) as second,
    ):
        assert first._producer is None
        assert second._producer is None
        assert first._consumer.config["group.id"] == observer_group
        assert second._consumer.config["group.id"] == observer_group
        assert harness.producers == []

    with KafkaStore(
        client_config,
        topic="audit",
        store_id="team",
    ) as writer:
        assert writer._consumer.config["group.id"] == reader_group
        assert writer._producer is harness.producers[0]

    assert client_config["allow.auto.create.topics"] is True
    assert client_config["group.protocol"] == "consumer"
    assert client_config["enable.metrics.push"] is True
    for config in harness.admin_configs[:2]:
        assert config["enable.metrics.push"] is False
    for consumer in harness.consumers[:2]:
        assert consumer.config["enable.metrics.push"] is False
    for config in harness.admin_configs:
        assert config["allow.auto.create.topics"] is False
        assert "group.id" not in config
        assert "group.instance.id" not in config
        assert "group.protocol" not in config
    for consumer in harness.consumers:
        assert consumer.config["allow.auto.create.topics"] is False
        assert consumer.config["group.protocol"] == "classic"
        assert "group.instance.id" not in consumer.config

    producer_config = harness.producers[0].config
    assert producer_config["allow.auto.create.topics"] is False
    assert producer_config["acks"] == "all"
    assert producer_config["enable.idempotence"] is True
    assert "group.id" not in producer_config
    assert "group.instance.id" not in producer_config
    assert "group.protocol" not in producer_config


def test_kafka_require_existing_refuses_empty_topic_before_producer_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)

    with pytest.raises(
        IntegrityError,
        match="no materialized nodes",
    ):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            store_id="team",
            require_existing=True,
        )

    assert len(harness.consumers) == 1
    assert harness.consumers[0].closed
    assert harness.producers == []
    assert harness.messages == []


def test_kafka_require_existing_replays_identity_before_creating_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "existing"},
    )
    harness.append_node(root)

    with KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        require_existing=True,
    ) as store:
        assert store.get(root.id) == root
        assert store._next_offset == 1
        assert store._producer is harness.producers[0]


def test_kafka_require_existing_rejects_wrong_identity_before_producer_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "wrong-identity"},
    )
    harness.append_node(root, store_id="other")

    with pytest.raises(IntegrityError, match="wrong store key"):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            store_id="team",
            require_existing=True,
        )

    assert harness.consumers[0].closed
    assert harness.producers == []


def test_kafka_require_existing_rejects_corrupt_history_before_producer_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    harness.messages.append((b"team", b"{}"))

    with pytest.raises(IntegrityError, match="invalid envelope fields"):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            store_id="team",
            require_existing=True,
        )

    assert harness.consumers[0].closed
    assert harness.producers == []


def test_kafka_require_existing_reconnect_restores_previous_clients_if_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "existing-before-reconnect"},
    )
    harness.append_node(root)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        require_existing=True,
    )
    previous_consumer = store._consumer
    previous_producer = store._producer
    harness.messages.clear()

    with pytest.raises(IntegrityError, match="no materialized nodes"):
        store.reconnect()

    assert store._consumer is previous_consumer
    assert store._producer is previous_producer
    assert not previous_consumer.closed
    assert not previous_producer.closed
    assert harness.consumers[1].closed
    assert harness.producers == [previous_producer]
    assert store._nodes[root.id] == root
    store.close()


def test_kafka_producer_construction_failure_closes_validated_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "producer-failure"},
    )
    harness.append_node(root)
    harness.fail_producer_creation = True

    with pytest.raises(OSError, match="producer construction failed"):
        KafkaStore(
            {"bootstrap.servers": "unused"},
            topic="audit",
            store_id="team",
            require_existing=True,
        )

    assert harness.consumers[0].closed
    assert harness.producers == []


def test_kafka_read_only_defends_all_write_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "snapshot"})
    child = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"write": "blocked"},
    )
    harness.append_node(root)

    with KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        read_only=True,
    ) as store:
        consumer = harness.consumers[0]
        assert store.get(root.id) == root
        assert store._producer is None
        before = (
            len(harness.messages),
            consumer.watermark_calls,
            consumer.poll_calls,
        )
        with pytest.raises(PermissionError, match="read-only"):
            store.put(child)
        with pytest.raises(PermissionError, match="read-only"):
            store.update_meta(root.id, {"blocked": True})
        with pytest.raises(PermissionError, match="read-only"):
            store._append_command("put", {})
        with pytest.raises(PermissionError, match="read-only"):
            store._produce_once(b"event")
        assert (
            len(harness.messages),
            consumer.watermark_calls,
            consumer.poll_calls,
        ) == before
        assert harness.producers == []


def test_kafka_read_only_snapshot_is_frozen_until_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "snapshot"})
    child = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"visible": "after reconnect"},
    )
    harness.append_node(root)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        read_only=True,
    )
    try:
        first_consumer = harness.consumers[0]
        assert store._snapshot_high_offset == 1
        assert first_consumer.watermark_calls == 1
        assert first_consumer.poll_calls == 1

        harness.append_node(child)
        assert not store.exists(child.id)
        assert store.children(root.id) == []
        assert first_consumer.watermark_calls == 1
        assert first_consumer.poll_calls == 1

        store.reconnect()

        second_consumer = harness.consumers[1]
        assert first_consumer.closed
        assert store._consumer is second_consumer
        assert not second_consumer.closed
        assert store._producer is None
        assert store._snapshot_high_offset == 2
        assert second_consumer.watermark_calls == 1
        assert second_consumer.poll_calls == 2
        assert store.get(child.id) == child
        assert harness.producers == []
    finally:
        store.close()


@pytest.mark.parametrize("read_only", [False, True], ids=["writer", "observer"])
def test_kafka_reconnect_atomically_replaces_clients(
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        read_only=read_only,
    )
    previous_consumer = store._consumer
    previous_producer = store._producer
    previous_group = previous_consumer.config["group.id"]

    store.reconnect()

    replacement_consumer = store._consumer
    replacement_producer = store._producer
    assert replacement_consumer is not previous_consumer
    assert replacement_consumer.config["group.id"] == previous_group
    assert previous_consumer.closed
    assert not replacement_consumer.closed
    if read_only:
        assert previous_producer is None
        assert replacement_producer is None
        assert harness.producers == []
    else:
        assert replacement_producer is not previous_producer
        assert previous_producer.closed
        assert not replacement_producer.closed

    store.close()
    assert replacement_consumer.closed
    if replacement_producer is not None:
        assert replacement_producer.closed


@pytest.mark.parametrize("read_only", [False, True], ids=["writer", "observer"])
def test_kafka_failed_reconnect_restores_clients_and_view(
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "atomic"})
    child = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"unseen": True},
    )
    harness.append_node(root)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        read_only=read_only,
    )
    previous_consumer = store._consumer
    previous_producer = store._producer
    previous_view = (
        store._nodes,
        store._children,
        store._operations,
        store._operation_offsets,
        store._outcomes,
        store._event_digests,
        store._next_offset,
        store._snapshot_high_offset,
    )
    harness.append_node(child)
    harness.fail_watermark_indices.add(1)

    with pytest.raises(IntegrityError, match="watermarks"):
        store.reconnect()

    replacement_consumer = harness.consumers[1]
    assert store._consumer is previous_consumer
    assert store._producer is previous_producer
    assert not previous_consumer.closed
    assert replacement_consumer.closed
    assert store._nodes is previous_view[0]
    assert store._children is previous_view[1]
    assert store._operations is previous_view[2]
    assert store._operation_offsets is previous_view[3]
    assert store._outcomes is previous_view[4]
    assert store._event_digests is previous_view[5]
    assert store._next_offset == previous_view[6]
    assert store._snapshot_high_offset == previous_view[7]
    assert root.id in store._nodes
    assert child.id not in store._nodes
    if read_only:
        assert previous_producer is None
        assert harness.producers == []
    else:
        assert not previous_producer.closed
        assert harness.producers == [previous_producer]

    store.close()
    assert previous_consumer.closed
    if previous_producer is not None:
        assert previous_producer.closed


@pytest.mark.parametrize("read_only", [False, True], ids=["writer", "observer"])
@pytest.mark.parametrize("change", ["truncate", "rewrite"])
def test_kafka_reconnect_refuses_changed_history_and_restores_previous_view(
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    change: str,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "original"})
    harness.append_node(root)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
        read_only=read_only,
    )
    previous_consumer = store._consumer
    previous_producer = store._producer
    previous_nodes = store._nodes
    previous_digests = store._event_digests

    harness.messages.clear()
    if change == "rewrite":
        replacement = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "replacement"},
        )
        harness.append_node(replacement)

    with pytest.raises(IntegrityError, match="history changed"):
        store.reconnect()

    failed_consumer = harness.consumers[1]
    assert store._consumer is previous_consumer
    assert store._producer is previous_producer
    assert store._nodes is previous_nodes
    assert store._event_digests is previous_digests
    assert store._nodes[root.id] == root
    assert not previous_consumer.closed
    assert failed_consumer.closed
    if read_only:
        assert previous_producer is None
        assert harness.producers == []
    else:
        assert not previous_producer.closed
        assert harness.producers == [previous_producer]

    store.close()
    assert previous_consumer.closed
    if previous_producer is not None:
        assert previous_producer.closed


def test_kafka_acknowledged_write_repair_failure_restores_and_closes_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_fake_kafka(monkeypatch)
    store = KafkaStore(
        {"bootstrap.servers": "unused"},
        topic="audit",
        store_id="team",
    )
    previous_consumer = store._consumer
    previous_producer = store._producer
    previous_nodes = store._nodes

    def acknowledge_then_break_replay(
        *,
        value: bytes,
        operation_id: str,
    ) -> int:
        assert operation_id
        harness.messages.append((b"team", value))
        harness.fail_poll_indices.add(0)
        harness.fail_watermark_indices.add(1)
        return 0

    monkeypatch.setattr(
        store,
        "_produce_with_recovery",
        acknowledge_then_break_replay,
    )
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "repair"})

    with pytest.raises(
        IntegrityError,
        match="acknowledged but replay confirmation failed",
    ):
        store.put(root)

    failed_consumer = harness.consumers[1]
    assert store._consumer is previous_consumer
    assert store._producer is previous_producer
    assert store._nodes is previous_nodes
    assert root.id not in store._nodes
    assert not previous_consumer.closed
    assert not previous_producer.closed
    assert failed_consumer.closed
    assert harness.producers == [previous_producer]

    store.close()
    assert previous_consumer.closed
    assert previous_producer.closed


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


def test_kafka_real_read_only_snapshot_refreshes_only_on_reconnect() -> None:
    with _topic() as topic, _open(topic) as writer:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "observer"})
        writer.put(root)
        observer_config = {
            "bootstrap.servers": os.environ["POLLARD_TEST_KAFKA_BOOTSTRAP"]
        }
        with KafkaStore(
            observer_config,
            topic=topic,
            read_only=True,
        ) as observer:
            child = Node.make(
                kind=NodeKind.NOTE,
                parent=root.id,
                payload={"visible": "after reconnect"},
            )
            writer.put(child)

            assert observer._producer is None
            assert not observer.exists(child.id)
            with pytest.raises(PermissionError, match="read-only"):
                observer.put(child)
            with pytest.raises(PermissionError, match="read-only"):
                observer.update_meta(root.id, {"blocked": True})

            observer.reconnect()

            assert observer._producer is None
            assert observer.get(child.id) == child


def test_kafka_real_cli_observation_export_import_and_merge_are_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bootstrap = os.environ.get("POLLARD_TEST_KAFKA_BOOTSTRAP")
    if not bootstrap:
        pytest.skip("Kafka is not configured")
    from confluent_kafka.admin import AdminClient  # type: ignore[attr-defined]

    store_id = f"cli-{uuid4().hex}"
    variable = "POLLARD_CLI_TEST_KAFKA_CONFIG"
    client_config = {"bootstrap.servers": bootstrap}
    monkeypatch.setenv(variable, json.dumps(client_config))
    admin = AdminClient(
        {
            **client_config,
            "allow.auto.create.topics": False,
        }
    )

    with _topic() as topic, KafkaStore(
        client_config,
        topic=topic,
        store_id=store_id,
    ) as writer:
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": f"cli-kafka-{store_id}"},
        )
        child = Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock-1", "prompt": "Kafka CLI acceptance"},
            result={
                "text": "stored result",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
            meta={"charges": {"tokens": 3}},
        )
        writer.put(root)
        writer.put(child)
        partition = writer._kafka.TopicPartition(topic, 0)
        low, high_before = writer._consumer.get_watermark_offsets(
            partition,
            timeout=10,
            cached=False,
        )
        assert (int(low), int(high_before)) == (0, 2)

        spec = f"kafka-env:{variable}?topic={topic}#{store_id}"
        label = f"kafka-env:{variable}?topic={topic}#{store_id}"
        transcript: list[str] = []

        assert main(["show", spec, root.id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        shown = json.loads(captured.out)
        assert shown["root_id"] == root.id
        assert len(shown["nodes"]) == 2

        assert main(["report", spec, root.id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        reported = json.loads(captured.out)
        assert reported["nodes"] == 2
        assert reported["spent"]["tokens"] == 3

        assert main(["verify", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        verified = json.loads(captured.out)
        assert verified["ok"] is True
        assert verified["roots"] == [root.id]
        assert verified["nodes"] == 2

        assert main(["seal", spec, root.id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        sealed = json.loads(captured.out)
        assert sealed["root_id"] == root.id
        assert len(sealed["digest"]) == 64

        export_path = tmp_path / "kafka-acceptance.json"
        assert main(["export", spec, root.id, str(export_path), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        exported = json.loads(captured.out)
        assert exported["digest"] == sealed["digest"]
        assert exported["nodes"] == 2

        assert main(["runs", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        listed = json.loads(captured.out)
        assert listed["runs"] == [
            {
                "attempt": 0,
                "label": f"cli-kafka-{store_id}",
                "nodes": 2,
                "pruned": 0,
                "root_id": root.id,
                "store": label,
            }
        ]

        imported_db = tmp_path / "kafka-imported.db"
        assert main(["import", str(export_path), str(imported_db), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["imported"] == 2
        with SQLiteStore(imported_db, read_only=True) as imported:
            assert imported.get(root.id) == root
            assert imported.get(child.id) == child

        merged_db = tmp_path / "kafka-merged.db"
        assert main(["merge", str(merged_db), spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        merged = json.loads(captured.out)
        assert merged["copied"] == 2
        assert merged["sources"][0]["source"] == label
        with SQLiteStore(merged_db, read_only=True) as merged_store:
            assert merged_store.get(root.id) == root
            assert merged_store.get(child.id) == child

        low_after, high_after = writer._consumer.get_watermark_offsets(
            partition,
            timeout=10,
            cached=False,
        )
        assert (int(low_after), int(high_after)) == (0, int(high_before))

        missing_topic = f"pollard-kafka-missing-{uuid4().hex}"
        assert missing_topic not in admin.list_topics(timeout=10).topics
        missing_spec = (
            f"kafka-env:{variable}?topic={missing_topic}#{store_id}"
        )
        assert main(["runs", missing_spec, "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        missing_exists = missing_topic in admin.list_topics(timeout=10).topics
        if missing_exists:
            admin.delete_topics([missing_topic])[missing_topic].result(timeout=10)
        assert not missing_exists

        _low_final, high_final = writer._consumer.get_watermark_offsets(
            partition,
            timeout=10,
            cached=False,
        )
        assert int(high_final) == int(high_before)
        assert bootstrap not in "".join(transcript)


def test_kafka_real_cli_merge_destination_is_existing_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bootstrap = os.environ.get("POLLARD_TEST_KAFKA_BOOTSTRAP")
    if not bootstrap:
        pytest.skip("Kafka is not configured")
    from confluent_kafka import Consumer, TopicPartition
    from confluent_kafka.admin import AdminClient  # type: ignore[attr-defined]

    variable = "POLLARD_CLI_DESTINATION_TEST_KAFKA_CONFIG"
    client_config = {"bootstrap.servers": bootstrap}
    monkeypatch.setenv(variable, json.dumps(client_config))
    admin = AdminClient(
        {
            **client_config,
            "allow.auto.create.topics": False,
        }
    )

    def watermark(topic: str) -> tuple[int, int]:
        consumer = Consumer(
            {
                **client_config,
                "group.id": f"pollard-watermark-{uuid4().hex}",
                "allow.auto.create.topics": False,
            }
        )
        try:
            low, high = consumer.get_watermark_offsets(
                TopicPartition(topic, 0),
                timeout=10,
                cached=False,
            )
            return int(low), int(high)
        finally:
            consumer.close()

    source_path = tmp_path / "kafka-destination-source.db"
    source_root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "kafka-destination-source"},
    )
    source_child = Node.make(
        kind=NodeKind.NOTE,
        parent=source_root.id,
        payload={"copied": True},
        meta={"charges": {"tokens": 2}},
    )
    with SQLiteStore(source_path) as source:
        source.put(source_root)
        source.put(source_child)
    missing_source = tmp_path / "kafka-destination-missing.db"
    store_id = f"destination-{uuid4().hex}"
    transcript: list[str] = []

    with _topic() as empty_topic, _topic() as destination_topic:
        empty_spec = (
            f"kafka-env:{variable}?topic={empty_topic}#{store_id}"
        )
        assert watermark(empty_topic) == (0, 0)
        assert main(["merge", empty_spec, str(source_path), "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "no materialized nodes" in captured.err
        assert watermark(empty_topic) == (0, 0)

        missing_topic = f"pollard-kafka-missing-{uuid4().hex}"
        assert missing_topic not in admin.list_topics(timeout=10).topics
        missing_spec = (
            f"kafka-env:{variable}?topic={missing_topic}#{store_id}"
        )
        assert main(["merge", missing_spec, str(source_path), "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert missing_topic not in admin.list_topics(timeout=10).topics

        anchor = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "existing-kafka-destination"},
        )
        with KafkaStore(
            client_config,
            topic=destination_topic,
            store_id=store_id,
        ) as seed:
            seed.put(anchor)
        destination_spec = (
            f"kafka-env:{variable}?topic={destination_topic}#{store_id}"
        )
        assert watermark(destination_topic) == (0, 1)

        assert (
            main(
                [
                    "merge",
                    destination_spec,
                    str(source_path),
                    str(missing_source),
                    "--json",
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "kafka-destination-missing.db" in captured.err
        assert watermark(destination_topic) == (0, 1)

        assert (
            main(
                [
                    "merge",
                    destination_spec,
                    str(source_path),
                    "--json",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        first = json.loads(captured.out)
        assert first["copied"] == 2
        assert first["existing"] == 0
        before_repeat = watermark(destination_topic)
        assert before_repeat == (0, 3)

        with KafkaStore(
            client_config,
            topic=destination_topic,
            store_id=store_id,
            read_only=True,
            require_existing=True,
        ) as reopened:
            assert reopened.get(anchor.id) == anchor
            assert reopened.get(source_root.id) == source_root
            assert reopened.get(source_child.id) == source_child
            assert verify(reopened).ok
            assert seal(reopened, source_root.id).root_id == source_root.id

        assert (
            main(
                [
                    "merge",
                    destination_spec,
                    str(source_path),
                    "--json",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        repeated = json.loads(captured.out)
        assert repeated["copied"] == 0
        assert repeated["existing"] == 2
        assert repeated["result_conflicts"] == 0
        assert repeated["meta_conflicts"] == 0
        assert watermark(destination_topic) == before_repeat
        assert bootstrap not in "".join(transcript)


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
