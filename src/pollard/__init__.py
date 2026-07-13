"""Governed execution trees for AI agents: budget it, gate it, replay it."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

if TYPE_CHECKING:
    from .aio import AsyncRun, AsyncRuntime
    from .errors import (
        BudgetExceeded,
        ConfirmationRequired,
        IntegrityError,
        MissingRecording,
        PolicyViolation,
        PollardError,
        UnsupportedSchema,
    )
    from .governor import Budget, recompute_charges
    from .policy import Decision, Policy, PolicyContext
    from .registry import ActionSpec, Registry
    from .replay import ReplayMode
    from .runtime import Run, Runtime
    from .store import MemoryStore
    from .stores import SQLiteStore
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
    "MemoryStore": ("pollard.store", "MemoryStore"),
    "MissingRecording": ("pollard.errors", "MissingRecording"),
    "Node": ("pollard.tree", "Node"),
    "NodeKind": ("pollard.tree", "NodeKind"),
    "Policy": ("pollard.policy", "Policy"),
    "PolicyContext": ("pollard.policy", "PolicyContext"),
    "PolicyViolation": ("pollard.errors", "PolicyViolation"),
    "PollardError": ("pollard.errors", "PollardError"),
    "Registry": ("pollard.registry", "Registry"),
    "ReplayMode": ("pollard.replay", "ReplayMode"),
    "Run": ("pollard.runtime", "Run"),
    "Runtime": ("pollard.runtime", "Runtime"),
    "SQLiteStore": ("pollard.stores", "SQLiteStore"),
    "UnsupportedSchema": ("pollard.errors", "UnsupportedSchema"),
    "VerifyFinding": ("pollard.verify", "VerifyFinding"),
    "VerifyReport": ("pollard.verify", "VerifyReport"),
    "recompute_charges": ("pollard.governor", "recompute_charges"),
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
    "IntegrityError",
    "MemoryStore",
    "MissingRecording",
    "Node",
    "NodeKind",
    "Policy",
    "PolicyContext",
    "PolicyViolation",
    "PollardError",
    "Registry",
    "ReplayMode",
    "Run",
    "Runtime",
    "SQLiteStore",
    "UnsupportedSchema",
    "VerifyFinding",
    "VerifyReport",
    "__version__",
    "recompute_charges",
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
