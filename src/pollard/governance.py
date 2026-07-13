"""Offline storage governance operations."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from ._canon import canonical_bytes
from .errors import IntegrityError
from .seal import seal
from .store import Store
from .tree import Node

EXPORT_FORMAT = "pollard/subtree/v1"
GCMode = Literal["drop-pruned", "compact"]


@dataclass(frozen=True)
class GCReport:
    mode: GCMode
    removed_nodes: tuple[str, ...]
    removed_blobs: int
    survivor_seals: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "removed_nodes": len(self.removed_nodes),
            "removed_node_ids": list(self.removed_nodes),
            "removed_blobs": self.removed_blobs,
            "survivor_seals": self.survivor_seals,
        }


@dataclass(frozen=True)
class ExportReport:
    path: str
    root_id: str
    digest: str
    nodes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "root_id": self.root_id,
            "digest": self.digest,
            "nodes": self.nodes,
        }


@dataclass(frozen=True)
class ImportReport:
    path: str
    root_id: str
    digest: str
    imported: int
    existing: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "root_id": self.root_id,
            "digest": self.digest,
            "imported": self.imported,
            "existing": self.existing,
        }


@runtime_checkable
class _MaintenanceStore(Protocol):
    def _pollard_drop_nodes(self, node_ids: set[str]) -> None: ...

    def _pollard_compact(self) -> int: ...


def gc(store: Store, *, mode: GCMode = "drop-pruned") -> GCReport:
    """Run an explicit offline garbage-collection operation."""

    if not isinstance(store, _MaintenanceStore):
        raise TypeError("store backend does not support offline garbage collection")
    _seal_roots(store)
    removed: set[str] = set()
    removed_blobs = 0
    if mode == "drop-pruned":
        for root_id in store.roots():
            for node in list(store.walk(root_id)):
                if node.id not in removed and node.meta.get("pruned") is True:
                    removed.update(child.id for child in store.walk(node.id))
        store._pollard_drop_nodes(removed)
    elif mode == "compact":
        removed_blobs = store._pollard_compact()
    else:
        raise ValueError(f"unsupported gc mode: {mode}")
    survivors = _seal_roots(store)
    return GCReport(
        mode=mode,
        removed_nodes=tuple(sorted(removed)),
        removed_blobs=removed_blobs,
        survivor_seals=survivors,
    )


def export_subtree(store: Store, root_id: str, path: str | Path) -> ExportReport:
    """Write a sealed, self-contained subtree manifest."""

    report = seal(store, root_id)
    nodes = list(store.walk(root_id))
    manifest = {
        "format": EXPORT_FORMAT,
        "root_id": root_id,
        "seal": report.to_dict(),
        "nodes": [_node_record(node) for node in nodes],
    }
    output = Path(path)
    output.write_text(_json_text(manifest, indent=2) + "\n", encoding="utf-8")
    return ExportReport(
        path=str(output),
        root_id=root_id,
        digest=report.digest,
        nodes=len(nodes),
    )


def import_subtree(path: str | Path, store: Store) -> ImportReport:
    """Verify a sealed subtree manifest completely before writing any node."""

    source = Path(path)
    manifest = json.loads(source.read_text(encoding="utf-8"))
    root_id, nodes, expected_seal = _parse_manifest(manifest)
    staged = _ManifestStore(root_id, nodes)
    actual_seal = seal(staged, root_id)
    if actual_seal.to_dict() != expected_seal:
        raise IntegrityError("subtree seal does not match the manifest")
    root = staged.get(root_id)
    if root.parent is not None and not store.exists(root.parent):
        raise IntegrityError(f"subtree parent is missing from target store: {root.parent}")
    existing = 0
    for node in nodes:
        if not store.exists(node.id):
            continue
        stored = store.get(node.id)
        if stored.identity_tuple() != node.identity_tuple():
            raise IntegrityError(f"target identity conflicts with imported node: {node.id}")
        if stored.result_text != node.result_text or stored.result_digest != node.result_digest:
            raise IntegrityError(f"target result conflicts with imported node: {node.id}")
        existing += 1
    imported = 0
    for node in nodes:
        if store.exists(node.id):
            continue
        store.put(node)
        imported += 1
    return ImportReport(
        path=str(source),
        root_id=root_id,
        digest=actual_seal.digest,
        imported=imported,
        existing=existing,
    )


def _seal_roots(store: Store) -> dict[str, str]:
    return {root_id: seal(store, root_id).digest for root_id in store.roots()}


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


def _parse_manifest(value: object) -> tuple[str, list[Node], dict[str, Any]]:
    if not isinstance(value, dict) or value.get("format") != EXPORT_FORMAT:
        raise IntegrityError("unsupported subtree export format")
    root_id = value.get("root_id")
    records = value.get("nodes")
    expected_seal = value.get("seal")
    if not isinstance(root_id, str) or not isinstance(records, list):
        raise IntegrityError("subtree manifest is missing its root or nodes")
    if not isinstance(expected_seal, dict):
        raise IntegrityError("subtree manifest is missing its seal")
    nodes = [_node_from_record(record) for record in records]
    if not nodes or nodes[0].id != root_id:
        raise IntegrityError("subtree manifest does not begin with its root")
    return root_id, nodes, expected_seal


def _node_from_record(value: object) -> Node:
    if not isinstance(value, dict):
        raise IntegrityError("subtree node record must be an object")
    try:
        return Node.from_storage(
            id=_required_str(value, "id"),
            parent=_optional_str(value, "parent"),
            kind=_required_str(value, "kind"),
            attempt=_required_int(value, "attempt"),
            payload_text=_required_str(value, "payload"),
            result_text=_optional_str(value, "result"),
            result_digest=_optional_str(value, "result_digest"),
            meta_text=_required_str(value, "meta"),
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityError(f"invalid subtree node record: {exc}") from exc


def _required_str(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise IntegrityError(f"subtree node {key} must be a string")
    return item


def _optional_str(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise IntegrityError(f"subtree node {key} must be a string or null")
    return item


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise IntegrityError(f"subtree node {key} must be an integer")
    return item


class _ManifestStore:
    def __init__(self, root_id: str, nodes: list[Node]) -> None:
        self._root_id = root_id
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[str]] = {}
        node_ids = [node.id for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise IntegrityError("subtree manifest contains duplicate node ids")
        if nodes[0].parent in set(node_ids):
            raise IntegrityError("subtree root parent must be outside the manifest")
        for index, node in enumerate(nodes):
            if index > 0 and node.parent not in self._nodes:
                raise IntegrityError(f"subtree node parent is outside the manifest: {node.id}")
            self._nodes[node.id] = node
            if node.parent is not None:
                self._children.setdefault(node.parent, []).append(node.id)
        walked = [node.id for node in self.walk(root_id)]
        if walked != [node.id for node in nodes]:
            raise IntegrityError("subtree nodes are not in deterministic walk order")

    def put(self, _node: Node) -> None:
        raise TypeError("manifest store is read-only")

    def get(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def exists(self, node_id: str) -> bool:
        return node_id in self._nodes

    def children(self, node_id: str) -> list[str]:
        return sorted(
            self._children.get(node_id, []),
            key=lambda item: (self._nodes[item].kind, item),
        )

    def update_meta(self, _node_id: str, _patch: dict[str, object]) -> None:
        raise TypeError("manifest store is read-only")

    def walk(self, root_id: str) -> Iterator[Node]:
        yield self.get(root_id)
        for child_id in self.children(root_id):
            yield from self.walk(child_id)

    def roots(self) -> list[str]:
        return [self._root_id]


def _json_text(value: object, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=None if indent is not None else (",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
    )
