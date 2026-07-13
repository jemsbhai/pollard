"""Governed execution trees for AI agents: budget it, gate it, replay it."""

__version__ = "0.0.1"

from .errors import BudgetExceeded, IntegrityError, PollardError
from .store import MemoryStore
from .stores import SQLiteStore
from .tree import Node, NodeKind
from .verify import VerifyFinding, VerifyReport, verify

__all__ = [
    "BudgetExceeded",
    "IntegrityError",
    "MemoryStore",
    "Node",
    "NodeKind",
    "PollardError",
    "SQLiteStore",
    "VerifyFinding",
    "VerifyReport",
    "__version__",
    "verify",
]
