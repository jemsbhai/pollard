"""Hashrope-backed store."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast

from hashrope import (  # type: ignore[import-untyped]
    PolynomialHash,
    rope_concat,
    rope_from_bytes,
    rope_hash,
    rope_to_bytes,
    validate_rope,
)

from pollard._canon import canonical_bytes
from pollard.errors import IntegrityError
from pollard.store import _validate_for_put
from pollard.tree import Node


class HashRopeStore:
    """Append-only store whose operation log is held in a hashrope."""

    def __init__(self, data: bytes = b"") -> None:
        self._hasher = PolynomialHash()
        self._rope: Any = rope_from_bytes(data, self._hasher)
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, set[str]] = {}
        self._replay(data)

    def put(self, node: Node) -> None:
        if self._apply_put(node):
            self._append({"op": "put", **_node_record(node)})

    def get(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def exists(self, node_id: str) -> bool:
        return node_id in self._nodes

    def children(self, node_id: str) -> list[str]:
        return sorted(
            self._children.get(node_id, set()),
            key=lambda item: (self._nodes[item].kind, item),
        )

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        self._apply_meta(node_id, patch)
        self._append({"op": "meta", "id": node_id, "patch": patch})

    def walk(self, root_id: str) -> Iterator[Node]:
        root = self.get(root_id)
        yield root
        for child_id in self.children(root_id):
            yield from self.walk(child_id)

    def roots(self) -> list[str]:
        return sorted(
            (node_id for node_id, node in self._nodes.items() if node.parent is None),
            key=lambda item: (str(self._nodes[item].payload.get("run", "")), item),
        )

    def to_bytes(self) -> bytes:
        """Return the current append-only log bytes."""

        return cast(bytes, rope_to_bytes(self._rope))

    def content_hash(self) -> int:
        """Return hashrope's polynomial hash for the operation log."""

        return cast(int, rope_hash(self._rope))

    def validate_log(self) -> None:
        """Validate hashrope invariants for the operation log."""

        validate_rope(self._rope, self._hasher)

    def _append(self, record: dict[str, Any]) -> None:
        line = (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        self._rope = rope_concat(self._rope, rope_from_bytes(line, self._hasher), self._hasher)

    def _replay(self, data: bytes) -> None:
        for line_number, line in enumerate(data.splitlines(), start=1):
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise IntegrityError(f"hashrope log line {line_number} is not an object")
            op = record.get("op")
            if op == "put":
                self._apply_put(_node_from_record(record))
                continue
            if op == "meta":
                node_id = record.get("id")
                patch = record.get("patch")
                if not isinstance(node_id, str) or not isinstance(patch, dict):
                    raise IntegrityError(f"hashrope log line {line_number} has invalid meta patch")
                self._apply_meta(node_id, patch)
                continue
            raise IntegrityError(f"hashrope log line {line_number} has unknown op {op!r}")

    def _apply_put(self, node: Node) -> bool:
        _validate_for_put(node)
        if node.parent is not None and node.parent not in self._nodes:
            raise KeyError(node.parent)
        existing = self._nodes.get(node.id)
        if existing is not None:
            return self._handle_existing(existing, node)
        self._nodes[node.id] = node
        if node.parent is not None:
            self._children.setdefault(node.parent, set()).add(node.id)
        return True

    def _handle_existing(self, existing: Node, incoming: Node) -> bool:
        if existing.identity_tuple() != incoming.identity_tuple():
            raise IntegrityError(f"node id collision for {incoming.id}")
        if incoming.result_text is None or incoming.result_text == existing.result_text:
            return False
        conflicts = list(existing.meta.get("result_conflicts", []))
        conflicts.append({"result_digest": incoming.result_digest, "result": incoming.result})
        self._nodes[existing.id] = replace(
            existing,
            meta={**existing.meta, "result_conflicts": conflicts},
        )
        return True

    def _apply_meta(self, node_id: str, patch: dict[str, object]) -> None:
        node = self._nodes[node_id]
        self._nodes[node_id] = replace(node, meta={**node.meta, **patch})


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


def _node_from_record(record: dict[str, Any]) -> Node:
    try:
        return Node.from_storage(
            id=str(record["id"]),
            parent=None if record["parent"] is None else str(record["parent"]),
            kind=str(record["kind"]),
            attempt=int(record["attempt"]),
            payload_text=str(record["payload"]),
            result_text=None if record["result"] is None else str(record["result"]),
            result_digest=(
                None if record["result_digest"] is None else str(record["result_digest"])
            ),
            meta_text=str(record["meta"]),
        )
    except KeyError as exc:
        raise IntegrityError(f"hashrope log put record missing {exc.args[0]}") from exc


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
