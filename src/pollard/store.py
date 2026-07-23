"""Store protocol and in-memory backend."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from typing import Protocol

from .errors import IntegrityError
from .hashing import result_digest_from_text
from .tree import Node, NodeKind


class Store(Protocol):
    def put(self, node: Node) -> None: ...

    def get(self, node_id: str) -> Node: ...

    def exists(self, node_id: str) -> bool: ...

    def children(self, node_id: str) -> list[str]: ...

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None: ...

    def walk(self, root_id: str) -> Iterator[Node]: ...

    def roots(self) -> list[str]: ...


class MemoryStore:
    """Append-only in-memory store for tests and ephemeral runs."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, set[str]] = {}

    def put(self, node: Node) -> None:
        _validate_for_put(node)
        if node.parent is not None and node.parent not in self._nodes:
            raise KeyError(node.parent)
        existing = self._nodes.get(node.id)
        if existing is not None:
            self._handle_existing(existing, node)
            return
        self._nodes[node.id] = _copy_node(node)
        if node.parent is not None:
            self._children.setdefault(node.parent, set()).add(node.id)

    def get(self, node_id: str) -> Node:
        return _copy_node(self._nodes[node_id])

    def exists(self, node_id: str) -> bool:
        return node_id in self._nodes

    def children(self, node_id: str) -> list[str]:
        return sorted(
            self._children.get(node_id, set()),
            key=lambda item: (self._nodes[item].kind, item),
        )

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        node = self._nodes[node_id]
        self._nodes[node_id] = replace(
            node,
            meta=deepcopy({**node.meta, **patch}),
        )

    def walk(self, root_id: str) -> Iterator[Node]:
        pending = [root_id]
        while pending:
            node_id = pending.pop()
            yield self.get(node_id)
            pending.extend(reversed(self.children(node_id)))

    def roots(self) -> list[str]:
        return sorted(
            (node_id for node_id, node in self._nodes.items() if node.parent is None),
            key=lambda item: (str(self._nodes[item].payload.get("run", "")), item),
        )

    def _pollard_drop_nodes(self, node_ids: set[str]) -> None:
        self._nodes = {
            node_id: node for node_id, node in self._nodes.items() if node_id not in node_ids
        }
        self._children = {}
        for node in self._nodes.values():
            if node.parent is not None:
                self._children.setdefault(node.parent, set()).add(node.id)

    def _pollard_compact(self) -> int:
        return 0

    def _handle_existing(self, existing: Node, incoming: Node) -> None:
        if existing.identity_tuple() != incoming.identity_tuple():
            raise IntegrityError(f"node id collision for {incoming.id}")
        if incoming.result_text is None or incoming.result_text == existing.result_text:
            return
        conflicts = list(existing.meta.get("result_conflicts", []))
        conflicts.append(
            {
                "result_digest": incoming.result_digest,
                "result": deepcopy(incoming.result),
            }
        )
        self._nodes[existing.id] = replace(
            existing,
            meta={**existing.meta, "result_conflicts": conflicts},
        )


def _validate_for_put(node: Node) -> None:
    if node.id != node.expected_id:
        raise IntegrityError(f"node id does not match identity fields: {node.id}")
    if node.result_text is not None and node.result_digest is None:
        raise IntegrityError(f"node result digest missing: {node.id}")
    if (
        node.result_text is not None
        and node.result_digest != result_digest_from_text(node.result_text)
    ):
        raise IntegrityError(f"node result digest does not match result: {node.id}")
    if node.kind == NodeKind.ROOT.value and node.parent is not None:
        raise IntegrityError("root node cannot have a parent")


def _copy_node(node: Node) -> Node:
    """Return a detached node so callers cannot mutate stored state."""

    return deepcopy(node)
