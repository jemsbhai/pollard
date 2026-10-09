"""Offline team accounting from declared actor anchors and recorded node metadata."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from .dependencies import DependencyFinding, DependencyReport, _read_tree, verify_dependencies
from .governor import charge_to_json
from .hashing import result_digest_from_text
from .store import Store
from .team_context import AgentIdentity
from .tree import Node, NodeKind


@dataclass(frozen=True)
class TeamMetrics:
    governed_calls: int
    model_calls: int
    tool_calls: int
    refusals: int
    dry_runs: int
    failed_outcomes: int
    charges: dict[str, int | float]
    usage: dict[str, int | float]
    summed_call_duration_seconds: int | float
    dependency_notes: int
    dependency_references: int
    handoffs: int


@dataclass(frozen=True)
class AgentReport:
    team_id: str
    agent_id: str
    task_id: str
    role: str | None
    metrics: TeamMetrics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeamReportFinding:
    node_id: str
    code: str
    message: str


@dataclass(frozen=True)
class TeamReport:
    root_id: str
    totals: TeamMetrics
    agents: list[AgentReport]
    unattributed: TeamMetrics
    unattributed_node_ids: list[str]
    orphaned_node_ids: list[str]
    dependencies: DependencyReport
    findings: list[TeamReportFinding]

    @property
    def ok(self) -> bool:
        return not self.findings and self.dependencies.ok

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["ok"] = self.ok
        result["dependencies"] = self.dependencies.to_dict()
        result["interpretation"] = {
            "identity": "caller-declared, not authenticated",
            "accounting": "recorded mutable metadata, not seal-protected",
            "duration": "sum of recorded call durations, not elapsed time or critical path",
            "calls": "recorded governed calls including dry runs and post-dispatch failures",
            "usage": "each usage field is summed independently; overlapping fields are not added",
        }
        return result


def team_report(store: Store, root_id: str) -> TeamReport:
    """Attribute each node once to its nearest enclosing declared team actor.

    Nested delegates do not contribute their charges to their parent's group.
    Usage fields are flattened with dotted paths and summed independently.
    Replays do not add new recorded calls. No quality, authenticated identity,
    critical path or end-to-end latency is inferred from this report.
    """

    dependency_report = verify_dependencies(store, root_id)
    totals = _MetricsBuilder()
    unattributed = _MetricsBuilder()
    groups: dict[tuple[str, str, str, str | None], _MetricsBuilder] = {}
    actors: dict[str, AgentIdentity | None] = {}
    orphans: dict[str, bool] = {}
    invalid: set[str] = set()
    findings: list[TeamReportFinding] = []
    traversal_findings: list[DependencyFinding] = []
    unattributed_ids: list[str] = []
    orphaned_ids: list[str] = []
    for node in _read_tree(store, root_id, traversal_findings):
        actor = actors.get(node.parent) if node.parent is not None else None
        orphan = orphans.get(node.parent, False) if node.parent is not None else False
        broken = node.id != node.expected_id or (
            node.result_text is not None
            and node.result_digest != result_digest_from_text(node.result_text)
        )
        broken = broken or (node.parent is not None and node.parent in invalid)
        if node.id == root_id and node.parent is not None:
            broken = True
        if node.id != root_id and node.parent not in actors:
            broken = True
        if broken:
            invalid.add(node.id)
            findings.append(TeamReportFinding(node.id, "invalid_node", "invalid node or ancestry"))
            actor, orphan = None, True
        reserved = node.payload.get("_pollard")
        if isinstance(reserved, dict) and "team_agent" in reserved:
            try:
                anchor = reserved["team_agent"]
                if node.kind != NodeKind.NOTE.value or not isinstance(anchor, dict):
                    raise ValueError("team actor anchor must be a note containing an object")
                if type(anchor.get("version")) is not int or anchor["version"] != 1:
                    raise ValueError("unsupported team actor anchor version")
                declared = AgentIdentity.from_dict(anchor.get("identity"))
                if declared.team_id != root_id:
                    raise ValueError("team actor anchor belongs to a different run")
                if not broken:
                    actor, orphan = declared, False
            except (TypeError, ValueError) as exc:
                findings.append(TeamReportFinding(node.id, "invalid_actor", str(exc)))
                actor, orphan = None, True
        actors[node.id], orphans[node.id] = actor, orphan
        totals.add(node)
        if actor is None:
            unattributed.add(node)
            if _is_work(node):
                unattributed_ids.append(node.id)
                if orphan:
                    orphaned_ids.append(node.id)
        else:
            key = (actor.team_id, actor.agent_id, actor.task_id, actor.role)
            groups.setdefault(key, _MetricsBuilder()).add(node)
    findings.extend(
        TeamReportFinding(item.node_id, item.code, item.message) for item in traversal_findings
    )
    agents = [
        AgentReport(*key, metrics=builder.finish())
        for key, builder in sorted(groups.items(), key=lambda item: tuple(v or "" for v in item[0]))
    ]
    return TeamReport(
        root_id,
        totals.finish(),
        agents,
        unattributed.finish(),
        sorted(unattributed_ids),
        sorted(orphaned_ids),
        dependency_report,
        findings,
    )


def _call_kind(node: Node) -> str | None:
    if node.kind in {NodeKind.MODEL_CALL.value, NodeKind.TOOL_CALL.value}:
        return node.kind
    if node.kind == NodeKind.NOTE.value and node.payload.get("event") in (
        "call_outcome_unknown", "call_recording_failed"
    ):
        kind = node.payload.get("blocked_kind")
        if kind in (NodeKind.MODEL_CALL.value, NodeKind.TOOL_CALL.value):
            return str(kind)
    return None


def _is_work(node: Node) -> bool:
    if _call_kind(node) is not None or node.kind == NodeKind.REFUSAL.value:
        return True
    reserved = node.payload.get("_pollard")
    return isinstance(reserved, dict) and "dependency" in reserved


@dataclass
class _MetricsBuilder:
    model_calls: int = 0
    tool_calls: int = 0
    refusals: int = 0
    dry_runs: int = 0
    failed_outcomes: int = 0
    charges: dict[str, Decimal] = field(default_factory=dict)
    usage: dict[str, Decimal] = field(default_factory=dict)
    duration: Decimal = Decimal("0")
    dependency_notes: int = 0
    dependency_references: int = 0
    handoffs: int = 0

    def add(self, node: Node) -> None:
        kind = _call_kind(node)
        if kind is not None:
            self.model_calls += kind == NodeKind.MODEL_CALL.value
            self.tool_calls += kind == NodeKind.TOOL_CALL.value
            self.dry_runs += node.meta.get("dry_run") is True
            self.failed_outcomes += node.kind == NodeKind.NOTE.value
            duration = _number(node.meta.get("duration_s"))
            if duration is not None:
                self.duration += duration
            usage = node.meta.get("usage")
            if not isinstance(usage, dict) and isinstance(node.result, dict):
                usage = node.result.get("usage")
            _add_numbers(self.usage, usage, nested=True)
        self.refusals += node.kind == NodeKind.REFUSAL.value
        _add_numbers(self.charges, node.meta.get("charges"))
        reserved = node.payload.get("_pollard")
        if isinstance(reserved, dict) and "dependency" in reserved:
            self.dependency_notes += 1
            dependency = reserved["dependency"]
            if isinstance(dependency, dict):
                self.handoffs += dependency.get("relation") == "handoff"
                references = dependency.get("references")
                if isinstance(references, list):
                    self.dependency_references += len(references)

    def finish(self) -> TeamMetrics:
        return TeamMetrics(
            governed_calls=self.model_calls + self.tool_calls,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            refusals=self.refusals,
            dry_runs=self.dry_runs,
            failed_outcomes=self.failed_outcomes,
            charges={key: charge_to_json(value) for key, value in sorted(self.charges.items())},
            usage={key: charge_to_json(value) for key, value in sorted(self.usage.items())},
            summed_call_duration_seconds=charge_to_json(self.duration),
            dependency_notes=self.dependency_notes,
            dependency_references=self.dependency_references,
            handoffs=self.handoffs,
        )


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return Decimal(str(value)) if value >= 0 else None


def _add_numbers(
    totals: dict[str, Decimal], value: object, *, nested: bool = False, prefix: str = ""
) -> None:
    if not isinstance(value, dict):
        return
    for name, amount in value.items():
        if not isinstance(name, str):
            continue
        key = f"{prefix}.{name}" if prefix else name
        number = _number(amount)
        if number is not None:
            totals[key] = totals.get(key, Decimal("0")) + number
        elif nested and isinstance(amount, dict):
            _add_numbers(totals, amount, nested=True, prefix=key)
