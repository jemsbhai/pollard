"""SQLite-backed store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from pollard._canon import canonical_bytes
from pollard.errors import IntegrityError
from pollard.store import _validate_for_put
from pollard.tree import Node


class SQLiteStore:
    """Single-writer SQLite store with WAL enabled."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
              id            TEXT PRIMARY KEY,
              parent        TEXT,
              kind          TEXT NOT NULL,
              attempt       INTEGER NOT NULL,
              payload       TEXT NOT NULL,
              result        TEXT,
              result_digest TEXT,
              meta          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            """
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        self._conn.commit()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def put(self, node: Node) -> None:
        _validate_for_put(node)
        if node.parent is not None and not self.exists(node.parent):
            raise KeyError(node.parent)
        existing = self._get_optional(node.id)
        if existing is not None:
            self._handle_existing(existing, node)
            return
        self._conn.execute(
            """
            INSERT INTO nodes (id, parent, kind, attempt, payload, result, result_digest, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.id,
                node.parent,
                node.kind,
                node.attempt,
                canonical_bytes(node.payload).decode("utf-8"),
                node.result_text,
                node.result_digest,
                _json_text(node.meta),
            ),
        )
        self._conn.commit()

    def get(self, node_id: str) -> Node:
        node = self._get_optional(node_id)
        if node is None:
            raise KeyError(node_id)
        return node

    def exists(self, node_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row is not None

    def children(self, node_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM nodes WHERE parent = ? ORDER BY kind ASC, id ASC",
            (node_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        node = self.get(node_id)
        meta = {**node.meta, **patch}
        self._conn.execute("UPDATE nodes SET meta = ? WHERE id = ?", (_json_text(meta), node_id))
        self._conn.commit()

    def walk(self, root_id: str) -> Iterator[Node]:
        yield self.get(root_id)
        for child_id in self.children(root_id):
            yield from self.walk(child_id)

    def roots(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM nodes WHERE parent IS NULL ORDER BY payload ASC, id ASC"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _get_optional(self, node_id: str) -> Node | None:
        row = self._conn.execute(
            """
            SELECT id, parent, kind, attempt, payload, result, result_digest, meta
            FROM nodes
            WHERE id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return Node.from_storage(
            id=str(row[0]),
            parent=None if row[1] is None else str(row[1]),
            kind=str(row[2]),
            attempt=int(row[3]),
            payload_text=str(row[4]),
            result_text=None if row[5] is None else str(row[5]),
            result_digest=None if row[6] is None else str(row[6]),
            meta_text=str(row[7]),
        )

    def _handle_existing(self, existing: Node, incoming: Node) -> None:
        if existing.identity_tuple() != incoming.identity_tuple():
            raise IntegrityError(f"node id collision for {incoming.id}")
        if incoming.result_text is None or incoming.result_text == existing.result_text:
            return
        conflicts = list(existing.meta.get("result_conflicts", []))
        conflicts.append({"result_digest": incoming.result_digest, "result": incoming.result})
        self.update_meta(existing.id, {"result_conflicts": conflicts})


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
