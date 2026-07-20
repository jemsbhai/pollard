"""Governed execution trees for AI agents: budget it, gate it, replay it."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any

__version__ = "1.0.3"

if TYPE_CHECKING:
    from .aio import AsyncRun, AsyncRuntime
    from .errors import (
        BudgetExceeded,
        ConfirmationRequired,
        IntegrityError,
        MissingRecording,
        PolicyViolation,
        PollardError,
        ReservationLeaseLost,
        ReservationUncertain,
        SettlementUncertain,
        UnsupportedSchema,
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
    from .runtime import Run, Runtime
    from .seal import SealEntry, SealReport, seal
    from .seal_custody import SealCustodyRecord, SQLiteSealSink
    from .store import MemoryStore, Store
    from .stores import HashRopeStore, PostgresStore, SQLiteStore
    from .tree import Node, NodeKind
    from .verify import VerifyFinding, VerifyReport, verify

_EXPORTS = {
    "ActionSpec": ("pollard.registry", "ActionSpec"),
    "AsyncRun": ("pollard.aio", "AsyncRun"),
    "AsyncRuntime": ("pollard.aio", "AsyncRuntime"),
    "Budget": ("pollard.governor", "Budget"),
    "BudgetExceeded": ("pollard.errors", "BudgetExceeded"),
    "ConfirmationRequired": ("pollard.errors", "ConfirmationRequired"),
    "Decision": ("pollard.policy", "Decision"),
    "IntegrityError": ("pollard.errors", "IntegrityError"),
    "HashRopeStore": ("pollard.stores.hashrope", "HashRopeStore"),
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
    "PollardError": ("pollard.errors", "PollardError"),
    "Registry": ("pollard.registry", "Registry"),
    "ReservationLeaseLost": ("pollard.errors", "ReservationLeaseLost"),
    "ReservationUncertain": ("pollard.errors", "ReservationUncertain"),
    "ReplayMode": ("pollard.replay", "ReplayMode"),
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
}

__all__ = [
    "ActionSpec",
    "AsyncRun",
    "AsyncRuntime",
    "Budget",
    "BudgetExceeded",
    "ConfirmationRequired",
    "Decision",
    "ExportReport",
    "GCReport",
    "HashRopeStore",
    "ImportReport",
    "IntegrityError",
    "MemoryStore",
    "MergeReport",
    "MissingRecording",
    "Node",
    "NodeKind",
    "Policy",
    "PolicyContext",
    "PolicyViolation",
    "PollardError",
    "PostgresStore",
    "Registry",
    "ReplayMode",
    "ReservationLeaseLost",
    "ReservationUncertain",
    "Run",
    "Runtime",
    "SQLiteSealSink",
    "SQLiteStore",
    "SealCustodyRecord",
    "SealEntry",
    "SealReport",
    "SettlementUncertain",
    "Store",
    "UnsupportedSchema",
    "VerifyFinding",
    "VerifyReport",
    "WindowMeter",
    "__version__",
    "export_subtree",
    "gc",
    "import_subtree",
    "merge",
    "recompute_charges",
    "redact",
    "seal",
    "verify",
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
