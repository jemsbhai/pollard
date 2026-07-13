"""Governed execution trees for AI agents: budget it, gate it, replay it."""

__version__ = "0.1.0"

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
