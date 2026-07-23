"""Kafka-backed append-only store.

This backend deliberately implements only :class:`pollard.store.Store`.  A
Kafka topic orders commands, but Kafka transactions do not provide the
compare-and-swap semantics required by Pollard's shared transactional arbiter.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import replace
from importlib import import_module
from threading import Event, RLock
from typing import Any

from pollard._canon import canonical_bytes
from pollard.errors import IntegrityError
from pollard.store import _copy_node, _validate_for_put
from pollard.tree import Node

KAFKA_EVENT_VERSION = 1
_EVENT_FIELDS = {
    "version",
    "store_id",
    "operation_id",
    "operation",
    "body",
    "request_digest",
}
_NODE_FIELDS = {
    "id",
    "parent",
    "kind",
    "attempt",
    "payload",
    "result",
    "result_digest",
    "meta",
}
_OPERATIONS = {"put", "meta"}
_OPERATION_PREFIX = b"pollard/kafka-operation/v1\n"
_CONTROLLED_CONFIG = {
    "acks",
    "enable.auto.commit",
    "enable.auto.offset.store",
    "enable.idempotence",
    "enable.partition.eof",
    "group.id",
    "isolation.level",
    "auto.offset.reset",
}


class KafkaStore:
    """A logical Pollard store in one dedicated single-partition Kafka topic.

    The topic is the source of truth.  Each instance replays it into an
    in-memory view and consumes through its own command offset before returning.
    The topic must retain its complete history and must not use log compaction.

    ``KafkaStore`` is not a shared budget arbiter.  In particular it exposes no
    ``_pollard_reserve`` or ``_pollard_renew`` methods.
    """

    def __init__(
        self,
        client_config: Mapping[str, object],
        *,
        topic: str,
        store_id: str = "default",
        timeout: int | float = 30,
    ) -> None:
        if not isinstance(client_config, Mapping):
            raise TypeError("client_config must be a mapping")
        if not isinstance(topic, str) or not topic:
            raise ValueError("topic must be a non-empty string")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive")

        try:
            kafka = import_module("confluent_kafka")
            kafka_admin = import_module("confluent_kafka.admin")
        except ImportError as exc:
            raise ImportError(
                "KafkaStore requires the 'kafka' extra: "
                "pip install 'pollard[kafka]'"
            ) from exc

        self.client_config = dict(client_config)
        if not self.client_config.get("bootstrap.servers"):
            raise ValueError("client_config requires 'bootstrap.servers'")
        if "transactional.id" in self.client_config:
            raise ValueError(
                "KafkaStore does not accept transactional.id; commands use "
                "application-level idempotency"
            )
        self.topic = topic
        self.store_id = store_id
        self.timeout = float(timeout)
        self._kafka = kafka
        self._kafka_admin = kafka_admin
        self._lock = RLock()
        self._closed = False
        self._producer: Any = None
        self._consumer: Any = None
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, set[str]] = {}
        self._operations: dict[str, str] = {}
        self._operation_offsets: dict[str, int] = {}
        self._outcomes: dict[str, tuple[str, str | None]] = {}
        self._next_offset = 0

        try:
            self._open_clients()
        except BaseException:
            self._shutdown_clients()
            raise

    def __enter__(self) -> KafkaStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the producer and consumer.  Calling ``close`` twice is safe."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_clients()

    def reconnect(self) -> None:
        """Replace both clients, revalidate the topic, and replay from offset zero."""

        with self._lock:
            self._require_open()
            self._shutdown_clients()
            self._reset_view()
            try:
                self._open_clients()
            except BaseException:
                self._shutdown_clients()
                raise

    def put(self, node: Node) -> None:
        _validate_for_put(node)
        body = _node_record(node)
        with self._lock:
            self._require_open()
            self._sync_current()
            if node.parent is not None and node.parent not in self._nodes:
                raise KeyError(node.parent)
            existing = self._nodes.get(node.id)
            if (
                existing is not None
                and existing.identity_tuple() != node.identity_tuple()
            ):
                raise IntegrityError(f"node id collision for {node.id}")
            operation_id = self._append_command("put", body)
            self._raise_outcome(operation_id)

    def get(self, node_id: str) -> Node:
        with self._lock:
            self._require_open()
            self._sync_current()
            return _copy_node(self._nodes[node_id])

    def exists(self, node_id: str) -> bool:
        with self._lock:
            self._require_open()
            self._sync_current()
            return node_id in self._nodes

    def children(self, node_id: str) -> list[str]:
        with self._lock:
            self._require_open()
            self._sync_current()
            return sorted(
                self._children.get(node_id, set()),
                key=lambda item: (self._nodes[item].kind, item),
            )

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        if not isinstance(node_id, str):
            raise TypeError("node_id must be a string")
        if not isinstance(patch, dict):
            raise TypeError("metadata patch must be a dictionary")
        # Validate JSON before dispatch so a serialization error cannot have an
        # ambiguous broker outcome.
        _json_bytes(patch)
        body: dict[str, Any] = {"id": node_id, "patch": patch}
        with self._lock:
            self._require_open()
            self._sync_current()
            if node_id not in self._nodes:
                raise KeyError(node_id)
            operation_id = self._append_command("meta", body)
            self._raise_outcome(operation_id)

    def walk(self, root_id: str) -> Iterator[Node]:
        with self._lock:
            self._require_open()
            self._sync_current()
            ordered: list[Node] = []
            pending = [root_id]
            while pending:
                node_id = pending.pop()
                ordered.append(_copy_node(self._nodes[node_id]))
                children = sorted(
                    self._children.get(node_id, set()),
                    key=lambda item: (self._nodes[item].kind, item),
                )
                pending.extend(reversed(children))
        return iter(ordered)

    def roots(self) -> list[str]:
        with self._lock:
            self._require_open()
            self._sync_current()
            return sorted(
                (
                    node_id
                    for node_id, node in self._nodes.items()
                    if node.parent is None
                ),
                key=lambda item: (str(self._nodes[item].payload.get("run", "")), item),
            )

    def _open_clients(self) -> None:
        self._validate_topic()
        base_config = {
            key: value
            for key, value in self.client_config.items()
            if key not in _CONTROLLED_CONFIG
        }
        producer_config = {
            **base_config,
            "acks": "all",
            "enable.idempotence": True,
        }
        consumer_config = {
            **base_config,
            "group.id": "pollard-" + hashlib.sha256(
                f"{self.topic}\0{self.store_id}\0{id(self)}".encode()
            ).hexdigest(),
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "enable.partition.eof": False,
            "auto.offset.reset": "earliest",
            "isolation.level": "read_committed",
        }
        self._producer = self._kafka.Producer(producer_config)
        self._consumer = self._kafka.Consumer(consumer_config)
        self._consumer.assign(
            [
                self._kafka.TopicPartition(
                    self.topic,
                    0,
                    self._kafka.OFFSET_BEGINNING,
                )
            ]
        )
        self._sync_current()

    def _shutdown_clients(self) -> None:
        consumer, producer = self._consumer, self._producer
        self._consumer = None
        self._producer = None
        if consumer is not None:
            with suppress(Exception):
                consumer.close()
        if producer is not None:
            close = getattr(producer, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            else:
                with suppress(Exception):
                    producer.flush(0)

    def _reset_view(self) -> None:
        self._nodes = {}
        self._children = {}
        self._operations = {}
        self._operation_offsets = {}
        self._outcomes = {}
        self._next_offset = 0

    def _validate_topic(self) -> None:
        admin = self._kafka_admin.AdminClient(
            {
                key: value
                for key, value in self.client_config.items()
                if key not in _CONTROLLED_CONFIG
            }
        )
        try:
            # Passing a topic name to list_topics can auto-create it on some
            # clusters.  Fetching all metadata keeps topic creation explicit.
            metadata = admin.list_topics(timeout=self.timeout)
        except BaseException as exc:
            raise IntegrityError("Kafka topic metadata could not be confirmed") from exc
        topic_metadata = getattr(metadata, "topics", {}).get(self.topic)
        if topic_metadata is None:
            raise IntegrityError(f"Kafka topic does not exist: {self.topic}")
        topic_error = getattr(topic_metadata, "error", None)
        if topic_error is not None:
            raise IntegrityError(
                f"Kafka topic metadata is unavailable for {self.topic}: {topic_error}"
            )
        partitions = getattr(topic_metadata, "partitions", None)
        if not isinstance(partitions, Mapping) or set(partitions) != {0}:
            raise IntegrityError(
                "KafkaStore requires a dedicated topic with exactly partition 0"
            )

        try:
            resource = self._kafka_admin.ConfigResource(
                self._kafka_admin.ResourceType.TOPIC,
                self.topic,
            )
            future = admin.describe_configs(
                [resource], request_timeout=self.timeout
            )[resource]
            described = future.result(timeout=self.timeout)
        except BaseException as exc:
            raise IntegrityError("Kafka topic configuration could not be confirmed") from exc
        values = {
            str(name): str(getattr(entry, "value", entry))
            for name, entry in described.items()
        }
        cleanup = {
            item.strip()
            for item in values.get("cleanup.policy", "").split(",")
            if item.strip()
        }
        if cleanup != {"delete"}:
            raise IntegrityError(
                "KafkaStore requires cleanup.policy=delete without log compaction"
            )
        for name in ("retention.ms", "retention.bytes"):
            if values.get(name) != "-1":
                raise IntegrityError(f"KafkaStore requires {name}=-1")

    def _append_command(self, operation: str, body: dict[str, Any]) -> str:
        self._sync_current()
        event, operation_id = _event(self.store_id, operation, body)
        if operation_id in self._operations:
            return operation_id
        value = _json_bytes(event)
        try:
            target_offset = self._produce_with_recovery(
                value=value,
                operation_id=operation_id,
            )
        except IntegrityError:
            raise
        except BaseException as exc:
            raise IntegrityError(
                f"Kafka write outcome is uncertain for operation {operation_id}"
            ) from exc

        try:
            self._sync_to_offset(target_offset)
        except BaseException as first_error:
            # The broker acknowledged the command.  Rebuild the view once so a
            # transient consumer failure cannot turn a committed command into an
            # unexplained application result.
            try:
                self._shutdown_clients()
                self._reset_view()
                self._open_clients()
            except BaseException as replay_error:
                raise IntegrityError(
                    "Kafka command was acknowledged but replay confirmation failed "
                    f"for operation {operation_id}"
                ) from replay_error
            if operation_id not in self._operations:
                raise IntegrityError(
                    "Kafka acknowledged an operation that was absent after replay: "
                    f"{operation_id}"
                ) from first_error
        return operation_id

    def _produce_with_recovery(self, *, value: bytes, operation_id: str) -> int:
        last_error: BaseException | None = None
        for _attempt in range(2):
            try:
                return self._produce_once(value)
            except BaseException as exc:
                last_error = exc
                with suppress(BaseException):
                    self._sync_current()
                known_offset = self._operation_offsets.get(operation_id)
                if known_offset is not None:
                    return known_offset
        with suppress(BaseException):
            self._sync_current()
        known_offset = self._operation_offsets.get(operation_id)
        if known_offset is not None:
            return known_offset
        error = IntegrityError(
            f"Kafka write outcome is uncertain for operation {operation_id}"
        )
        if last_error is None:
            raise error
        raise error from last_error

    def _produce_once(self, value: bytes) -> int:
        delivered = Event()
        result: dict[str, Any] = {}

        def on_delivery(error: object, message: Any) -> None:
            result["error"] = error
            result["message"] = message
            delivered.set()

        try:
            self._producer.produce(
                self.topic,
                key=self.store_id.encode("utf-8"),
                value=value,
                partition=0,
                on_delivery=on_delivery,
            )
        except BaseException as exc:
            raise IntegrityError("Kafka command could not be enqueued") from exc

        deadline = time.monotonic() + self.timeout
        while not delivered.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Kafka delivery acknowledgement timed out")
            self._producer.poll(min(0.1, remaining))
        error = result.get("error")
        if error is not None:
            raise self._kafka.KafkaException(error)
        message = result.get("message")
        if message is None:
            raise IntegrityError("Kafka delivery callback returned no message")
        if message.topic() != self.topic or int(message.partition()) != 0:
            raise IntegrityError("Kafka delivered a command to an unexpected location")
        offset = int(message.offset())
        if offset < 0:
            raise IntegrityError("Kafka delivered a command without a valid offset")
        return offset

    def _sync_current(self) -> None:
        try:
            low, high = self._consumer.get_watermark_offsets(
                self._kafka.TopicPartition(self.topic, 0),
                timeout=self.timeout,
                cached=False,
            )
        except BaseException as exc:
            raise IntegrityError("Kafka watermarks could not be confirmed") from exc
        low_offset, high_offset = int(low), int(high)
        if low_offset != 0:
            raise IntegrityError(
                "Kafka log start is not offset zero; Pollard history was truncated"
            )
        if high_offset < self._next_offset:
            raise IntegrityError("Kafka high watermark moved behind the replay cursor")
        if high_offset > self._next_offset:
            self._sync_to_offset(high_offset - 1)

    def _sync_to_offset(self, target_offset: int) -> None:
        if target_offset < self._next_offset:
            return
        deadline = time.monotonic() + self.timeout
        while self._next_offset <= target_offset:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IntegrityError(
                    f"Kafka replay timed out before offset {target_offset}"
                )
            try:
                message = self._consumer.poll(min(0.25, remaining))
            except BaseException as exc:
                raise IntegrityError("Kafka replay failed") from exc
            if message is None:
                continue
            error = message.error()
            if error is not None:
                raise IntegrityError(f"Kafka replay returned an error: {error}")
            if message.topic() != self.topic or int(message.partition()) != 0:
                raise IntegrityError("Kafka replay crossed the configured topic partition")
            offset = int(message.offset())
            if offset != self._next_offset:
                raise IntegrityError(
                    "Kafka log contains an offset gap: expected "
                    f"{self._next_offset}, received {offset}"
                )
            self._apply_message(offset, message.key(), message.value())
            self._next_offset += 1

    def _apply_message(self, offset: int, key: object, value: object) -> None:
        expected_key = self.store_id.encode("utf-8")
        if key != expected_key:
            raise IntegrityError(f"Kafka log offset {offset} has the wrong store key")
        if not isinstance(value, bytes):
            raise IntegrityError(f"Kafka log offset {offset} is not a byte record")
        event = _parse_event(value, offset=offset, store_id=self.store_id)
        operation_id = event["operation_id"]
        request_digest = event["request_digest"]
        if not isinstance(operation_id, str) or not isinstance(request_digest, str):
            raise IntegrityError(f"Kafka log offset {offset} has invalid digests")
        previous = self._operations.get(operation_id)
        if previous is not None:
            if previous != request_digest:
                raise IntegrityError(
                    f"Kafka operation id collision at offset {offset}: {operation_id}"
                )
            return
        self._operations[operation_id] = request_digest
        self._operation_offsets[operation_id] = offset
        operation = event["operation"]
        body = event["body"]
        if not isinstance(operation, str) or not isinstance(body, dict):
            raise IntegrityError(f"Kafka log offset {offset} has invalid command data")
        if operation == "put":
            node = _node_from_record(body, offset=offset)
            self._apply_put(operation_id, node)
            return
        if operation == "meta":
            self._apply_meta(operation_id, body, offset=offset)
            return
        raise IntegrityError(f"Kafka log offset {offset} has unknown operation")

    def _apply_put(self, operation_id: str, node: Node) -> None:
        _validate_for_put(node)
        if node.parent is not None and node.parent not in self._nodes:
            self._outcomes[operation_id] = ("key_error", node.parent)
            return
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
            if node.parent is not None:
                self._children.setdefault(node.parent, set()).add(node.id)
            self._outcomes[operation_id] = ("ok", None)
            return
        if existing.identity_tuple() != node.identity_tuple():
            self._outcomes[operation_id] = (
                "integrity_error",
                f"node id collision for {node.id}",
            )
            return
        if node.result_text is not None and node.result_text != existing.result_text:
            conflicts = list(existing.meta.get("result_conflicts", []))
            conflicts.append(
                {"result_digest": node.result_digest, "result": node.result}
            )
            self._nodes[node.id] = replace(
                existing,
                meta={**existing.meta, "result_conflicts": conflicts},
            )
        self._outcomes[operation_id] = ("ok", None)

    def _apply_meta(
        self,
        operation_id: str,
        body: dict[str, Any],
        *,
        offset: int,
    ) -> None:
        if set(body) != {"id", "patch"}:
            raise IntegrityError(f"Kafka log offset {offset} has invalid meta fields")
        node_id, patch = body.get("id"), body.get("patch")
        if not isinstance(node_id, str) or not isinstance(patch, dict):
            raise IntegrityError(f"Kafka log offset {offset} has invalid meta patch")
        node = self._nodes.get(node_id)
        if node is None:
            self._outcomes[operation_id] = ("key_error", node_id)
            return
        self._nodes[node_id] = replace(node, meta={**node.meta, **patch})
        self._outcomes[operation_id] = ("ok", None)

    def _raise_outcome(self, operation_id: str) -> None:
        outcome = self._outcomes.get(operation_id)
        if outcome is None:
            raise IntegrityError(f"Kafka operation has no replay outcome: {operation_id}")
        kind, detail = outcome
        if kind == "ok":
            return
        if kind == "key_error":
            raise KeyError(detail)
        if kind == "integrity_error":
            raise IntegrityError(detail or "Kafka operation failed integrity validation")
        raise IntegrityError(f"Kafka operation has an unknown outcome: {kind}")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("KafkaStore is closed")


def _event(
    store_id: str,
    operation: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if operation not in _OPERATIONS:
        raise ValueError(f"unsupported KafkaStore operation: {operation}")
    request = {"operation": operation, "body": body}
    request_bytes = _json_bytes(request)
    request_digest = hashlib.sha256(request_bytes).hexdigest()
    operation_id = hashlib.sha256(_OPERATION_PREFIX + request_bytes).hexdigest()
    return (
        {
            "version": KAFKA_EVENT_VERSION,
            "store_id": store_id,
            "operation_id": operation_id,
            "operation": operation,
            "body": body,
            "request_digest": request_digest,
        },
        operation_id,
    )


def _parse_event(value: bytes, *, offset: int, store_id: str) -> dict[str, Any]:
    try:
        decoded = value.decode("utf-8")
        event = json.loads(decoded, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrityError(f"Kafka log offset {offset} is not valid JSON") from exc
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise IntegrityError(f"Kafka log offset {offset} has invalid envelope fields")
    if event.get("version") != KAFKA_EVENT_VERSION or isinstance(
        event.get("version"), bool
    ):
        raise IntegrityError(f"Kafka log offset {offset} has unknown event version")
    if event.get("store_id") != store_id:
        raise IntegrityError(f"Kafka log offset {offset} belongs to another store")
    operation, body = event.get("operation"), event.get("body")
    if operation not in _OPERATIONS or not isinstance(body, dict):
        raise IntegrityError(f"Kafka log offset {offset} has invalid operation")
    expected, operation_id = _event(store_id, str(operation), body)
    if event != expected:
        raise IntegrityError(f"Kafka log offset {offset} failed envelope validation")
    if value != _json_bytes(event):
        raise IntegrityError(f"Kafka log offset {offset} is not canonically encoded")
    if event.get("operation_id") != operation_id:
        raise IntegrityError(f"Kafka log offset {offset} has invalid operation id")
    return event


def _node_record(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "parent": node.parent,
        "kind": node.kind,
        "attempt": node.attempt,
        "payload": canonical_bytes(node.payload).decode("utf-8"),
        "result": node.result_text,
        "result_digest": node.result_digest,
        "meta": _json_text(node.meta),
    }


def _node_from_record(record: dict[str, Any], *, offset: int) -> Node:
    if set(record) != _NODE_FIELDS:
        raise IntegrityError(f"Kafka log offset {offset} has invalid node fields")
    try:
        node = Node.from_storage(
            id=_required_str(record, "id"),
            parent=(
                None
                if record.get("parent") is None
                else _required_str(record, "parent")
            ),
            kind=_required_str(record, "kind"),
            attempt=_required_int(record, "attempt"),
            payload_text=_required_str(record, "payload"),
            result_text=(
                None
                if record.get("result") is None
                else _required_str(record, "result")
            ),
            result_digest=(
                None
                if record.get("result_digest") is None
                else _required_str(record, "result_digest")
            ),
            meta_text=_required_str(record, "meta"),
        )
        _validate_for_put(node)
        return node
    except (KeyError, TypeError, ValueError, IntegrityError) as exc:
        raise IntegrityError(f"Kafka log offset {offset} has an invalid node") from exc


def _required_str(value: dict[str, Any], name: str) -> str:
    item = value[name]
    if not isinstance(item, str):
        raise TypeError(f"{name} must be a string")
    return item


def _required_int(value: dict[str, Any], name: str) -> int:
    item = value[name]
    if isinstance(item, bool) or not isinstance(item, int):
        raise TypeError(f"{name} must be an integer")
    return int(item)


def _json_bytes(value: object) -> bytes:
    return _json_text(value).encode("utf-8")


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")
