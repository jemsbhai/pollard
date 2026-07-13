"""Governed execution trees for AI agents: budget it, gate it, replay it."""

__version__ = "0.2.0"

from .aio import AsyncRun, AsyncRuntime
from .errors import (
    BudgetExceeded,
    ConfirmationRequired,
    IntegrityError,
    PolicyViolation,
    PollardError,
    UnsupportedSchema,
)
from .governor import Budget, recompute_charges
from .policy import Decision, Policy, PolicyContext
from .registry import ActionSpec, Registry
from .runtime import Run, Runtime
from .store import MemoryStore
from .stores import SQLiteStore
from .tree import Node, NodeKind
from .verify import VerifyFinding, VerifyReport, verify

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
    "Node",
    "NodeKind",
    "Policy",
    "PolicyContext",
    "PolicyViolation",
    "PollardError",
    "Registry",
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
