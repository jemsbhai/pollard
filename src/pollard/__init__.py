"""Governed execution trees for AI agents: budget it, gate it, replay it."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any

__version__ = "1.6.1"

if TYPE_CHECKING:
    from .aio import AsyncRun, AsyncRuntime
    from .approvals import ApprovalPolicy, ApprovalRequest, decide_approval, request_approval
    from .dependencies import (
        DependencyFinding,
        DependencyReport,
        ResultReference,
        record_dependency,
        record_handoff,
        verify_dependencies,
    )
    from .errors import (
        BudgetExceeded,
        CallCleanupError,
        ConfirmationRequired,
        DuplicateRecording,
        IntegrityError,
        MissingRecording,
        PolicyViolation,
        PollardError,
        PostDispatchOutcomeUnknown,
        ReservationLeaseLost,
        ReservationUncertain,
        SettlementUncertain,
        UnsupportedSchema,
        is_post_dispatch_outcome_unknown,
        mark_post_dispatch_outcome_unknown,
    )
    from .governance import (
        ExportReport,
        GCReport,
        ImportReport,
        export_subtree,
        gc,
        import_subtree,
    )
    from .governor import Budget, recompute_charges
    from .merge import MergeReport, merge
    from .meters import WindowMeter
    from .policy import Decision, Policy, PolicyContext
    from .redaction import redact
    from .registry import ActionSpec, Registry
    from .replay import ReplayMode
    from .revalidation import (
        ExactResultComparator,
        NormalizedModelComparator,
        ReplayContract,
        RevalidationComparator,
        RevalidationComparison,
        RevalidationReport,
    )
    from .runtime import Run, Runtime
    from .scopes import SharedBudget
    from .seal import SealEntry, SealReport, seal
    from .seal_custody import SealCustodyRecord, SQLiteSealSink
    from .store import MemoryStore, Store
    from .stores import (
        HashRopeStore,
        KafkaStore,
        MongoStore,
        Neo4jStore,
        PostgresStore,
        RedisStore,
        SQLiteStore,
    )
    from .team_context import AgentCheckpoint, AgentIdentity, BudgetContext, DelegationContext
    from .team_reports import TeamReport, team_report
    from .teams import Team, TeamAgent
    from .tree import Node, NodeKind
    from .verify import VerifyFinding, VerifyReport, verify

_EXPORTS = {
    "AgentCheckpoint": ("pollard.team_context", "AgentCheckpoint"),
    "AgentIdentity": ("pollard.team_context", "AgentIdentity"),
    "ApprovalPolicy": ("pollard.approvals", "ApprovalPolicy"),
    "ApprovalRequest": ("pollard.approvals", "ApprovalRequest"),
    "BudgetContext": ("pollard.team_context", "BudgetContext"),
    "DelegationContext": ("pollard.team_context", "DelegationContext"),
    "DependencyFinding": ("pollard.dependencies", "DependencyFinding"),
    "DependencyReport": ("pollard.dependencies", "DependencyReport"),
    "ResultReference": ("pollard.dependencies", "ResultReference"),
    "SharedBudget": ("pollard.scopes", "SharedBudget"),
    "Team": ("pollard.teams", "Team"),
    "TeamAgent": ("pollard.teams", "TeamAgent"),
    "TeamReport": ("pollard.team_reports", "TeamReport"),
    "decide_approval": ("pollard.approvals", "decide_approval"),
    "request_approval": ("pollard.approvals", "request_approval"),
    "record_dependency": ("pollard.dependencies", "record_dependency"),
    "record_handoff": ("pollard.dependencies", "record_handoff"),
    "team_report": ("pollard.team_reports", "team_report"),
    "verify_dependencies": ("pollard.dependencies", "verify_dependencies"),
    "ActionSpec": ("pollard.registry", "ActionSpec"),
    "AsyncRun": ("pollard.aio", "AsyncRun"),
    "AsyncRuntime": ("pollard.aio", "AsyncRuntime"),
    "Budget": ("pollard.governor", "Budget"),
    "BudgetExceeded": ("pollard.errors", "BudgetExceeded"),
    "CallCleanupError": ("pollard.errors", "CallCleanupError"),
    "ConfirmationRequired": ("pollard.errors", "ConfirmationRequired"),
    "Decision": ("pollard.policy", "Decision"),
    "DuplicateRecording": ("pollard.errors", "DuplicateRecording"),
    "IntegrityError": ("pollard.errors", "IntegrityError"),
    "HashRopeStore": ("pollard.stores.hashrope", "HashRopeStore"),
    "KafkaStore": ("pollard.stores.kafka", "KafkaStore"),
    "MongoStore": ("pollard.stores.mongodb", "MongoStore"),
    "Neo4jStore": ("pollard.stores.neo4j", "Neo4jStore"),
    "ExportReport": ("pollard.governance", "ExportReport"),
    "GCReport": ("pollard.governance", "GCReport"),
    "ImportReport": ("pollard.governance", "ImportReport"),
    "MemoryStore": ("pollard.store", "MemoryStore"),
    "Store": ("pollard.store", "Store"),
    "MergeReport": ("pollard.merge", "MergeReport"),
    "MissingRecording": ("pollard.errors", "MissingRecording"),
    "Node": ("pollard.tree", "Node"),
    "NodeKind": ("pollard.tree", "NodeKind"),
    "Policy": ("pollard.policy", "Policy"),
    "PolicyContext": ("pollard.policy", "PolicyContext"),
    "PolicyViolation": ("pollard.errors", "PolicyViolation"),
    "PostgresStore": ("pollard.stores", "PostgresStore"),
    "RedisStore": ("pollard.stores.redis", "RedisStore"),
    "PollardError": ("pollard.errors", "PollardError"),
    "PostDispatchOutcomeUnknown": ("pollard.errors", "PostDispatchOutcomeUnknown"),
    "Registry": ("pollard.registry", "Registry"),
    "ReservationLeaseLost": ("pollard.errors", "ReservationLeaseLost"),
    "ReservationUncertain": ("pollard.errors", "ReservationUncertain"),
    "ReplayMode": ("pollard.replay", "ReplayMode"),
    "ReplayContract": ("pollard.revalidation", "ReplayContract"),
    "RevalidationComparator": ("pollard.revalidation", "RevalidationComparator"),
    "RevalidationComparison": ("pollard.revalidation", "RevalidationComparison"),
    "RevalidationReport": ("pollard.revalidation", "RevalidationReport"),
    "ExactResultComparator": ("pollard.revalidation", "ExactResultComparator"),
    "NormalizedModelComparator": (
        "pollard.revalidation",
        "NormalizedModelComparator",
    ),
    "Run": ("pollard.runtime", "Run"),
    "Runtime": ("pollard.runtime", "Runtime"),
    "SealEntry": ("pollard.seal", "SealEntry"),
    "SealReport": ("pollard.seal", "SealReport"),
    "SealCustodyRecord": ("pollard.seal_custody", "SealCustodyRecord"),
    "SQLiteSealSink": ("pollard.seal_custody", "SQLiteSealSink"),
    "SettlementUncertain": ("pollard.errors", "SettlementUncertain"),
    "SQLiteStore": ("pollard.stores", "SQLiteStore"),
    "UnsupportedSchema": ("pollard.errors", "UnsupportedSchema"),
    "VerifyFinding": ("pollard.verify", "VerifyFinding"),
    "VerifyReport": ("pollard.verify", "VerifyReport"),
    "WindowMeter": ("pollard.meters", "WindowMeter"),
    "recompute_charges": ("pollard.governor", "recompute_charges"),
    "export_subtree": ("pollard.governance", "export_subtree"),
    "gc": ("pollard.governance", "gc"),
    "import_subtree": ("pollard.governance", "import_subtree"),
    "merge": ("pollard.merge", "merge"),
    "redact": ("pollard.redaction", "redact"),
    "seal": ("pollard.seal", "seal"),
    "verify": ("pollard.verify", "verify"),
    "is_post_dispatch_outcome_unknown": (
        "pollard.errors",
        "is_post_dispatch_outcome_unknown",
    ),
    "mark_post_dispatch_outcome_unknown": (
        "pollard.errors",
        "mark_post_dispatch_outcome_unknown",
    ),
}

__all__ = [
    "ActionSpec",
    "AgentCheckpoint",
    "AgentIdentity",
    "ApprovalPolicy",
    "ApprovalRequest",
    "AsyncRun",
    "AsyncRuntime",
    "Budget",
    "BudgetContext",
    "BudgetExceeded",
    "CallCleanupError",
    "ConfirmationRequired",
    "Decision",
    "DelegationContext",
    "DependencyFinding",
    "DependencyReport",
    "DuplicateRecording",
    "ExactResultComparator",
    "ExportReport",
    "GCReport",
    "HashRopeStore",
    "ImportReport",
    "IntegrityError",
    "KafkaStore",
    "MemoryStore",
    "MergeReport",
    "MissingRecording",
    "MongoStore",
    "Neo4jStore",
    "Node",
    "NodeKind",
    "NormalizedModelComparator",
    "Policy",
    "PolicyContext",
    "PolicyViolation",
    "PollardError",
    "PostDispatchOutcomeUnknown",
    "PostgresStore",
    "RedisStore",
    "Registry",
    "ReplayContract",
    "ReplayMode",
    "ReservationLeaseLost",
    "ReservationUncertain",
    "ResultReference",
    "RevalidationComparator",
    "RevalidationComparison",
    "RevalidationReport",
    "Run",
    "Runtime",
    "SQLiteSealSink",
    "SQLiteStore",
    "SealCustodyRecord",
    "SealEntry",
    "SealReport",
    "SettlementUncertain",
    "SharedBudget",
    "Store",
    "Team",
    "TeamAgent",
    "TeamReport",
    "UnsupportedSchema",
    "VerifyFinding",
    "VerifyReport",
    "WindowMeter",
    "__version__",
    "decide_approval",
    "export_subtree",
    "gc",
    "import_subtree",
    "is_post_dispatch_outcome_unknown",
    "mark_post_dispatch_outcome_unknown",
    "merge",
    "recompute_charges",
    "record_dependency",
    "record_handoff",
    "redact",
    "request_approval",
    "seal",
    "team_report",
    "verify",
    "verify_dependencies",
]


def __getattr__(name: str) -> Any:
    return _load_export(name)


def _load_export(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module 'pollard' has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


class _PollardModule(ModuleType):
    def __dir__(self) -> list[str]:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        public = namespace.get("__all__", ())
        return sorted({*ModuleType.__dir__(self), *public})

    def __getattribute__(self, name: str) -> Any:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        exports = namespace.get("_EXPORTS", {})
        if name in exports:
            current = namespace.get(name)
            module_name, _attribute = exports[name]
            if current is None or (
                isinstance(current, ModuleType) and current.__name__ == module_name
            ):
                return namespace["_load_export"](name)
        return ModuleType.__getattribute__(self, name)


sys.modules[__name__].__class__ = _PollardModule
