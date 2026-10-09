"""Result dependencies recorded as immutable notes without changing tree identity."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from ._canon import IdentityValue
from .errors import IntegrityError
from .hashing import result_digest_from_text
from .runtime import Run
from .store import Store
from .tree import Node, NodeKind
from .verify import verify

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RESULT_KINDS = {NodeKind.MODEL_CALL.value, NodeKind.TOOL_CALL.value}


@dataclass(frozen=True)
class ResultReference:
    """Commit to one completed model or tool result, not merely its call identity."""

    node_id: str
    result_digest: str

    def __post_init__(self) -> None:
        for name in ("node_id", "result_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
                raise ValueError(f"{name} must be 64 lowercase hex characters")

    @classmethod
    def from_node(cls, node: Node) -> ResultReference:
        if node.kind not in _RESULT_KINDS or node.result_text is None:
            raise ValueError("result references require a completed model or tool call")
        if node.id != node.expected_id:
            raise IntegrityError("referenced node identity is invalid")
        if node.result_digest != result_digest_from_text(node.result_text):
            raise IntegrityError("referenced result digest is invalid")
        assert node.result_digest is not None
        return cls(node.id, node.result_digest)

    def to_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "result_digest": self.result_digest}


@dataclass(frozen=True)
class DependencyFinding:
    node_id: str
    code: str
    message: str
    target_id: str | None = None


@dataclass(frozen=True)
class DependencyReport:
    root_id: str
    checked_notes: int
    references: int
    findings: list[DependencyFinding]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


def record_dependency(
    run: Run,
    references: Iterable[ResultReference],
    *,
    relation: str = "consumes",
    recipient: str | None = None,
    task_id: str | None = None,
    label: str | None = None,
    attempt: int = 0,
) -> Node:
    """Record verified same-run result references, including across sibling branches.

    This is an audit record. It does not send a message or schedule the recipient.
    The same call in strict replay returns the recorded note without mutating it.
    Notes with no result cannot be used as result references.
    """

    unique: dict[str, ResultReference] = {}
    for reference in references:
        if not isinstance(reference, ResultReference):
            raise TypeError("references must contain ResultReference values")
        previous = unique.get(reference.node_id)
        if previous is not None and previous != reference:
            raise ValueError("conflicting result references for the same node")
        unique[reference.node_id] = reference
    refs = sorted(unique.values(), key=lambda item: item.node_id)
    document: dict[str, IdentityValue] = {
        "version": 1,
        "relation": relation,
        "references": [dict(reference.to_dict()) for reference in refs],
    }
    for name, value in (("recipient", recipient), ("task_id", task_id), ("label", label)):
        if value is not None:
            document[name] = value
    _parse_document(document)
    root = _verified_root(run.store, run.cursor_id)
    if root != run.root_id:
        raise IntegrityError("dependency cursor does not belong to the run root")
    for reference in refs:
        finding = _check_reference(run.store, run.root_id, run.cursor_id, reference)
        if finding is not None:
            raise IntegrityError(f"{finding.code}: {finding.message}")
    return run.note({"_pollard": {"dependency": document}}, attempt=attempt)


def record_handoff(
    run: Run,
    references: Iterable[ResultReference],
    *,
    recipient: str,
    task_id: str | None = None,
    label: str | None = None,
    attempt: int = 0,
) -> Node:
    """Record intended handoff of results; delivery remains application-owned."""

    return record_dependency(
        run,
        references,
        relation="handoff",
        recipient=recipient,
        task_id=task_id,
        label=label,
        attempt=attempt,
    )


def verify_dependencies(store: Store, root_id: str) -> DependencyReport:
    """Check immutable dependency notes and their referenced results.

    Verification includes note and target ancestry, same-run membership and the
    committed result digest. The report makes no claim about scheduling order or
    whether a recipient actually consumed the result.
    """

    findings: list[DependencyFinding] = []
    notes = 0
    references = 0
    try:
        if _verified_root(store, root_id) != root_id:
            raise IntegrityError("root_id must identify a run root")
    except (KeyError, IntegrityError, TypeError, ValueError) as exc:
        findings.append(DependencyFinding(root_id, "invalid_root", str(exc)))
        return DependencyReport(root_id, notes, references, findings)
    for node in _read_tree(store, root_id, findings):
        reserved = node.payload.get("_pollard")
        if not isinstance(reserved, dict) or "dependency" not in reserved:
            continue
        notes += 1
        try:
            if node.kind != NodeKind.NOTE.value:
                raise ValueError("dependency records must be notes")
            _, refs = _parse_document(reserved["dependency"])
        except (TypeError, ValueError) as exc:
            findings.append(DependencyFinding(node.id, "malformed_reference", str(exc)))
            continue
        try:
            if _verified_root(store, node.id) != root_id:
                raise IntegrityError("dependency note belongs to a different run")
        except (KeyError, IntegrityError, TypeError, ValueError) as exc:
            findings.append(DependencyFinding(node.id, "invalid_note", str(exc)))
        for reference in refs:
            references += 1
            finding = _check_reference(store, root_id, node.id, reference)
            if finding is not None:
                findings.append(finding)
    return DependencyReport(root_id, notes, references, findings)


def _parse_document(value: object) -> tuple[dict[str, Any], list[ResultReference]]:
    if not isinstance(value, dict):
        raise ValueError("dependency document must be an object")
    allowed = {"version", "relation", "references", "recipient", "task_id", "label"}
    if set(value) - allowed:
        raise ValueError("dependency document contains unsupported fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("unsupported dependency version")
    if value.get("relation") not in ("consumes", "handoff"):
        raise ValueError("dependency relation must be consumes or handoff")
    for name in ("recipient", "task_id", "label"):
        if name in value and (not isinstance(value[name], str) or not value[name].strip()):
            raise ValueError(f"dependency {name} must be a non-empty string")
    if value["relation"] == "handoff" and "recipient" not in value:
        raise ValueError("handoff requires a recipient")
    items = value.get("references")
    if not isinstance(items, list) or not items:
        raise ValueError("dependencies require at least one result reference")
    references: list[ResultReference] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"node_id", "result_digest"}:
            raise ValueError("result reference must contain node_id and result_digest")
        reference = ResultReference(item["node_id"], item["result_digest"])
        if reference.node_id in seen:
            raise ValueError("dependency contains duplicate result references")
        seen.add(reference.node_id)
        references.append(reference)
    return value, references


def _verified_root(store: Store, node_id: str) -> str:
    report = verify(store, node_id)
    if not report.ok:
        raise IntegrityError("; ".join(item.message for item in report.findings))
    node = store.get(node_id)
    while node.parent is not None:
        node = store.get(node.parent)
    return node.id


def _check_reference(
    store: Store, root_id: str, note_id: str, reference: ResultReference
) -> DependencyFinding | None:
    target_id = reference.node_id
    try:
        target = store.get(target_id)
    except KeyError:
        return DependencyFinding(note_id, "missing_target", "referenced node is missing", target_id)
    except (IntegrityError, TypeError, ValueError) as exc:
        return DependencyFinding(note_id, "invalid_target", str(exc), target_id)
    try:
        target_root = _verified_root(store, target_id)
    except (KeyError, IntegrityError, TypeError, ValueError) as exc:
        return DependencyFinding(note_id, "invalid_target", str(exc), target_id)
    if target_root != root_id:
        return DependencyFinding(
            note_id, "cross_run_reference", "referenced result belongs to another run", target_id
        )
    if target.kind not in _RESULT_KINDS or target.result_text is None:
        return DependencyFinding(
            note_id, "missing_result", "referenced node has no completed call result", target_id
        )
    if target.result_digest != reference.result_digest:
        return DependencyFinding(
            note_id, "changed_result", "referenced result differs from committed digest", target_id
        )
    return None


def _read_tree(
    store: Store, root_id: str, findings: list[DependencyFinding]
) -> Iterable[Node]:
    pending = [root_id]
    seen: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            findings.append(DependencyFinding(node_id, "invalid_tree", "repeated node in tree"))
            continue
        seen.add(node_id)
        try:
            node = store.get(node_id)
        except (KeyError, IntegrityError, TypeError, ValueError) as exc:
            findings.append(DependencyFinding(node_id, "invalid_node", str(exc)))
            continue
        yield node
        pending.extend(reversed(store.children(node_id)))
