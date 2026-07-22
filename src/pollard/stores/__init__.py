"""Store backends."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .sqlite import SQLiteStore

if TYPE_CHECKING:
    from .hashrope import HashRopeStore
    from .kafka import KafkaStore
    from .mongodb import MongoStore
    from .neo4j import Neo4jStore
    from .postgres import PostgresStore
    from .redis import RedisStore

__all__ = [
    "HashRopeStore",
    "KafkaStore",
    "MongoStore",
    "Neo4jStore",
    "PostgresStore",
    "RedisStore",
    "SQLiteStore",
]


def __getattr__(name: str) -> Any:
    if name == "HashRopeStore":
        return getattr(import_module("pollard.stores.hashrope"), name)
    if name == "PostgresStore":
        return getattr(import_module("pollard.stores.postgres"), name)
    if name == "RedisStore":
        return getattr(import_module("pollard.stores.redis"), name)
    if name == "MongoStore":
        return getattr(import_module("pollard.stores.mongodb"), name)
    if name == "KafkaStore":
        return getattr(import_module("pollard.stores.kafka"), name)
    if name == "Neo4jStore":
        return getattr(import_module("pollard.stores.neo4j"), name)
    raise AttributeError(f"module 'pollard.stores' has no attribute {name!r}")
