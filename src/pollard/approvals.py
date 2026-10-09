"""Durable, action-bound approval records for caller-controlled agent workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._canon import IdentityValue
from .dependencies import _verified_root
from .errors import IntegrityError
from .hashing import digest_payload
from .policy import Decision, PolicyContext
from .runtime import Run
from .store import Store
from .team_context import AgentIdentity, _node_id, _validate_name
from .tree import Node, NodeKind
from .verify import verify


@dataclass(frozen=True)
class ApprovalRequest:
    """An immutable request for one exact declared actor and registered action.

    ``id`` may be supplied to an external service as an idempotency key. Pollard
    does not make external side effects exactly once across concurrent workers.
    """

    id: str
    root_id: str
    parent_id: str
    name: str
    tool_version: str
    args_digest: str
    spec_digest: str
    registry_digest: str
    agent_identity: AgentIdentity | None
    attempt: int = 0

    def __post_init__(self) -> None:
        for name in ("id", "root_id", "parent_id", "args_digest", "spec_digest", "registry_digest"):
            _node_id(getattr(self, name), name)
        for name in ("name", "tool_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"approval {name} must be a non-empty string")
        if self.agent_identity is not None and not isinstance(self.agent_identity, AgentIdentity):
            raise TypeError("agent_identity must be AgentIdentity or None")
        if type(self.attempt) is not int or self.attempt < 0:
            raise ValueError("approval attempt must be a nonnegative integer")
        if self._node().id != self.id:
            raise IntegrityError("approval request ID does not match its contents")

    def _document(self) -> dict[str, IdentityValue]:
        return {
            "version": 1,
            "root_id": self.root_id,
            "name": self.name,
            "tool_version": self.tool_version,
            "args_digest": self.args_digest,
            "spec_digest": self.spec_digest,
            "registry_digest": self.registry_digest,
            "agent_identity": (
                None if self.agent_identity is None else self.agent_identity.to_dict()
            ),
        }

    def _node(self) -> Node:
        return Node.make(
            kind=NodeKind.NOTE, parent=self.parent_id, attempt=self.attempt,
            payload={"_pollard": {"approval_request": self._document()}},
        )

    def to_dict(self) -> dict[str, IdentityValue]:
        return {
            **self._document(), "id": self.id, "parent_id": self.parent_id, "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, value: object) -> ApprovalRequest:
        fields = {
            "version", "id", "root_id", "parent_id", "attempt", "name", "tool_version",
            "args_digest", "spec_digest", "registry_digest", "agent_identity",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid approval request fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported approval request version")
        identity = value["agent_identity"]
        return cls(
            id=value["id"], root_id=value["root_id"], parent_id=value["parent_id"],
            name=value["name"], tool_version=value["tool_version"],
            args_digest=value["args_digest"],
            spec_digest=value["spec_digest"], registry_digest=value["registry_digest"],
            agent_identity=None if identity is None else AgentIdentity.from_dict(identity),
            attempt=value["attempt"],
        )


def request_approval(
    run: Run,
    name: str,
    args: dict[str, IdentityValue],
    *,
    version: str | None = None,
    attempt: int = 0,
) -> ApprovalRequest:
    """Advance the cursor to an approval request without storing plaintext arguments.

    The registry validates arguments before their redacted audit representation
    is hashed. In strict replay the existing note is read without writing.
    """

    registry = run._runtime.registry
    if registry is None:
        raise ValueError("approval requests require a registry")
    spec = registry.get(name, version)
    error = spec.validate_args(args)
    if error is not None:
        raise ValueError(f"invalid approval arguments: {error}")
    if _verified_root(run.store, run.cursor_id) != run.root_id:
        raise IntegrityError("approval cursor does not belong to the run root")
    identity = run.agent_identity
    document: dict[str, IdentityValue] = {
        "version": 1, "root_id": run.root_id, "name": spec.name, "tool_version": spec.version,
        "args_digest": digest_payload(spec.redact_args(args)), "spec_digest": spec.spec_digest,
        "registry_digest": registry.registry_digest,
        "agent_identity": None if identity is None else identity.to_dict(),
    }
    payload: dict[str, IdentityValue] = {"_pollard": {"approval_request": document}}
    candidate = Node.make(
        kind=NodeKind.NOTE, parent=run.cursor_id, payload=payload, attempt=attempt
    )
    _request_from_node(candidate)
    node = run.note(payload, attempt=attempt)
    return _request_from_node(node)


def decide_approval(
    store: Store, request: ApprovalRequest, *, approved: bool, reviewer: str
) -> Node:
    """Persist one immutable decision, rejecting conflicting decisions.

    Reviewer names are caller declarations, not authenticated identities. An
    exact repeat returns the existing decision. A crash after slot creation can
    be repaired by repeating exactly the same decision. This is a write API;
    replay workflows only read requests and decisions.
    """

    _load_request(store, request)
    if type(approved) is not bool:
        raise TypeError("approved must be a boolean")
    _validate_name(reviewer, "reviewer")
    slot = _decision_slot(request)
    decision = Node.make(
        kind=NodeKind.NOTE, parent=slot.id,
        payload={"_pollard": {"approval_decision": {
            "version": 1, "request_id": request.id, "root_id": request.root_id,
            "approved": approved, "reviewer": reviewer,
        }}},
    )
    binding = Node.make(
        kind=slot.kind, parent=slot.parent, payload=slot.payload,
        result={"decision_id": decision.id},
    )
    if store.exists(slot.id):
        _validate_binding(store, binding)
        if store.exists(decision.id):
            found = _read_decision(store, request)
            assert found is not None
            return found
    if getattr(store, "read_only", False):
        raise RuntimeError("cannot record an approval decision in a read-only store")
    store.put(binding)
    _validate_binding(store, binding)
    store.put(decision)
    recorded = _read_decision(store, request)
    assert recorded is not None
    return recorded


class ApprovalPolicy:
    """Require a durable decision for the exact next governed action.

    Reads never mutate the store. Any recorded call or post-dispatch failure
    below a request exhausts it, including after rollback. Concurrent workers
    can still race this check, so external idempotency remains necessary.
    Missing or mismatched approval is denied; callers initiate review through
    request_approval(), not through the process-local confirmation-token API.
    """

    _pollard_durable_approval = True

    def __init__(self, store: Store, *, side_effects_only: bool = True) -> None:
        if type(side_effects_only) is not bool:
            raise TypeError("side_effects_only must be a boolean")
        self.store = store
        self.side_effects_only = side_effects_only

    def pollard_team_config(self) -> dict[str, IdentityValue]:
        return {"version": 1, "side_effects_only": self.side_effects_only}

    def decide(self, ctx: PolicyContext) -> Decision:
        if self.side_effects_only and not ctx.spec.side_effects:
            return Decision.ALLOW
        root_id = _verified_root(self.store, ctx.cursor_id)
        current = self.store.get(ctx.cursor_id)
        request: ApprovalRequest | None = None
        while True:
            if _is_dispatch(current):
                return Decision.DENY
            reserved = current.payload.get("_pollard")
            if isinstance(reserved, dict) and "approval_request" in reserved:
                request = _request_from_node(current)
                break
            if current.parent is None:
                return Decision.DENY
            current = self.store.get(current.parent)
        if (
            request.root_id != root_id
            or request.name != ctx.spec.name
            or request.tool_version != ctx.spec.version
            or request.spec_digest != ctx.spec.spec_digest
            or request.registry_digest != ctx.registry_digest
            or request.agent_identity != ctx.agent_identity
            or request.args_digest != digest_payload(ctx.spec.redact_args(ctx.args))
        ):
            return Decision.DENY
        _load_request(self.store, request)
        decision = _read_decision(self.store, request)
        if decision is None:
            return Decision.DENY
        document = _event(decision, "approval_decision")
        if document["approved"] is False:
            return Decision.DENY
        pending = [request.id]
        seen: set[str] = set()
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                raise IntegrityError("cycle in approval request subtree")
            seen.add(node_id)
            _verify_or_raise(self.store, node_id)
            node = self.store.get(node_id)
            if _is_dispatch(node):
                return Decision.DENY
            pending.extend(self.store.children(node_id))
        return Decision.ALLOW


def _is_dispatch(node: Node) -> bool:
    return node.kind in {NodeKind.MODEL_CALL.value, NodeKind.TOOL_CALL.value} or (
        node.kind == NodeKind.NOTE.value
        and node.payload.get("event") in ("call_outcome_unknown", "call_recording_failed")
    )


def _event(node: Node, name: str) -> dict[str, Any]:
    wrapper = node.payload.get("_pollard")
    document = wrapper.get(name) if isinstance(wrapper, dict) else None
    if node.kind != NodeKind.NOTE.value or not isinstance(document, dict):
        raise IntegrityError(f"invalid {name} note")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise IntegrityError(f"unsupported {name} version")
    return document


def _request_from_node(node: Node) -> ApprovalRequest:
    document = _event(node, "approval_request")
    request = ApprovalRequest.from_dict({
        **document, "id": node.id, "parent_id": node.parent, "attempt": node.attempt,
    })
    if node.identity_tuple() != request._node().identity_tuple() or node.result_text is not None:
        raise IntegrityError("invalid approval request note")
    return request


def _load_request(store: Store, request: ApprovalRequest) -> Node:
    if not isinstance(request, ApprovalRequest):
        raise TypeError("request must be an ApprovalRequest")
    if _verified_root(store, request.id) != request.root_id:
        raise IntegrityError("approval request belongs to a different root")
    node = store.get(request.id)
    if _request_from_node(node) != request:
        raise IntegrityError("approval request differs from its recording")
    return node


def _decision_slot(request: ApprovalRequest) -> Node:
    return Node.make(kind=NodeKind.NOTE, parent=request.id, payload={"_pollard": {
        "approval_decision_slot": {"version": 1, "request_id": request.id},
    }})


def _validate_binding(store: Store, expected: Node) -> Node:
    _verify_or_raise(store, expected.id)
    stored = store.get(expected.id)
    if stored.identity_tuple() != expected.identity_tuple() or stored.result != expected.result:
        raise IntegrityError("conflicting approval decision")
    return stored


def _read_decision(store: Store, request: ApprovalRequest) -> Node | None:
    expected = _decision_slot(request)
    if not store.exists(expected.id):
        return None
    _verify_or_raise(store, expected.id)
    slot = store.get(expected.id)
    if slot.identity_tuple() != expected.identity_tuple():
        raise IntegrityError("invalid approval decision slot")
    result = slot.result
    if not isinstance(result, dict) or set(result) != {"decision_id"}:
        raise IntegrityError("invalid approval decision binding")
    decision_id = _node_id(result["decision_id"], "decision_id")
    if not store.exists(decision_id):
        return None
    _verify_or_raise(store, decision_id)
    decision = store.get(decision_id)
    document = _event(decision, "approval_decision")
    if (
        set(document) != {"version", "request_id", "root_id", "approved", "reviewer"}
        or document["request_id"] != request.id
        or document["root_id"] != request.root_id
        or decision.parent != slot.id
        or type(document["approved"]) is not bool
    ):
        raise IntegrityError("invalid approval decision")
    _validate_name(document["reviewer"], "reviewer")
    return decision


def _verify_or_raise(store: Store, node_id: str) -> None:
    report = verify(store, node_id)
    if not report.ok:
        raise IntegrityError("; ".join(finding.message for finding in report.findings))
