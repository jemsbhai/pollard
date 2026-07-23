"""Integrity verification for pollard stores."""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import result_digest_from_text
from .store import Store


@dataclass(frozen=True)
class VerifyFinding:
    node_id: str
    message: str


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    findings: list[VerifyFinding]


def verify(store: Store, node_id: str) -> VerifyReport:
    """Verify a node and its ancestors."""

    findings: list[VerifyFinding] = []
    current_id: str | None = node_id
    seen: set[str] = set()
    while current_id is not None:
        if current_id in seen:
            findings.append(VerifyFinding(current_id, "cycle detected in ancestry"))
            break
        seen.add(current_id)
        try:
            node = store.get(current_id)
        except KeyError:
            findings.append(VerifyFinding(current_id, "node is missing"))
            break
        if node.id != node.expected_id:
            findings.append(VerifyFinding(node.id, "node id does not match identity fields"))
        if node.result_text is not None:
            expected_digest = result_digest_from_text(node.result_text)
            if node.result_digest != expected_digest:
                findings.append(
                    VerifyFinding(node.id, "result digest does not match stored result")
                )
        current_id = node.parent
    return VerifyReport(ok=not findings, findings=findings)
