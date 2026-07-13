"""Store backends."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .sqlite import SQLiteStore

if TYPE_CHECKING:
    from .hashrope import HashRopeStore
    from .postgres import PostgresStore

__all__ = ["HashRopeStore", "PostgresStore", "SQLiteStore"]


def __getattr__(name: str) -> Any:
    if name == "HashRopeStore":
        return getattr(import_module("pollard.stores.hashrope"), name)
    if name == "PostgresStore":
        return getattr(import_module("pollard.stores.postgres"), name)
    raise AttributeError(f"module 'pollard.stores' has no attribute {name!r}")
