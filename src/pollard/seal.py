"""Rolling result seals for exported Pollard subtrees."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ._canon import IdentityValue, canonical_bytes
from .errors import IntegrityError
from .hashing import result_digest_from_text
from .store import Store
from .tree import Node

SEAL_DOMAIN = b"pollard/v1:seal\n"
SEAL_ALGORITHM = "sha256:pollard/v1:seal"


@dataclass(frozen=True)
class SealEntry:
    index: int
    node_id: str
    parent_id: str | None
    kind: str
    result_digest: str | None
    previous: str | None
    seal: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "result_digest": self.result_digest,
            "previous": self.previous,
            "seal": self.seal,
        }


@dataclass(frozen=True)
class SealReport:
    root_id: str
    algorithm: str
    digest: str
    entries: tuple[SealEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "algorithm": self.algorithm,
            "digest": self.digest,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def seal(store: Store, root_id: str) -> SealReport:
    """Return a rolling seal over a subtree's node ids and result digests."""

    previous = ""
    entries: list[SealEntry] = []
    for index, node in enumerate(store.walk(root_id)):
        _validate_node(node)
        record = _seal_record(index=index, node=node, previous=previous)
        current = hashlib.sha256(SEAL_DOMAIN + canonical_bytes(record)).hexdigest()
        entries.append(
            SealEntry(
                index=index,
                node_id=node.id,
                parent_id=node.parent,
                kind=node.kind,
                result_digest=node.result_digest,
                previous=previous or None,
                seal=current,
            )
        )
        previous = current
    return SealReport(
        root_id=root_id,
        algorithm=SEAL_ALGORITHM,
        digest=previous,
        entries=tuple(entries),
    )


def _seal_record(index: int, node: Node, previous: str) -> IdentityValue:
    return {
        "index": index,
        "node_id": node.id,
        "parent_id": node.parent or "",
        "kind": node.kind,
        "result_digest": node.result_digest or "",
        "previous": previous,
    }


def _validate_node(node: Node) -> None:
    if node.id != node.expected_id:
        raise IntegrityError(f"node id does not match identity fields: {node.id}")
    if node.result_text is None:
        return
    expected_digest = result_digest_from_text(node.result_text)
    if node.result_digest != expected_digest:
        raise IntegrityError(f"node result digest does not match result: {node.id}")
