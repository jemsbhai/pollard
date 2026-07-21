"""SQLite-backed store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

from pollard._canon import IdentityValue, canonical_bytes
from pollard.arbiter import (
    BudgetReservation,
    ReservationCheck,
    WindowReservation,
)
from pollard.errors import IntegrityError
from pollard.store import _validate_for_put
from pollard.tree import Node


class SQLiteStore:
    """Transactional SQLite store with WAL enabled."""

    def __init__(
        self,
        path: str | Path,
        *,
        intern_payloads: bool = True,
        intern_threshold: int = 1024,
    ) -> None:
        if isinstance(intern_threshold, bool) or intern_threshold < 1:
            raise ValueError("intern_threshold must be a positive integer")
        self.path = Path(path)
        self.intern_payloads = intern_payloads
        self.intern_threshold = intern_threshold
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
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
            CREATE TABLE IF NOT EXISTS blobs (
              digest TEXT PRIMARY KEY,
              value  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blob_literals (
              node_id TEXT NOT NULL,
              path    TEXT NOT NULL,
              PRIMARY KEY (node_id, path)
            );
            CREATE TABLE IF NOT EXISTS budget_state (
              scope_id TEXT NOT NULL,
              meter    TEXT NOT NULL,
              settled  TEXT NOT NULL,
              PRIMARY KEY (scope_id, meter)
            );
            CREATE TABLE IF NOT EXISTS reservations (
              reservation_id TEXT NOT NULL,
              kind           TEXT NOT NULL,
              scope_id       TEXT NOT NULL,
              meter          TEXT NOT NULL,
              amount         TEXT NOT NULL,
              expires_at     REAL NOT NULL,
              window_seconds REAL,
              PRIMARY KEY (reservation_id, kind, scope_id, meter)
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_scope
              ON reservations(kind, scope_id, meter, expires_at);
            CREATE TABLE IF NOT EXISTS window_events (
              event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
              scope_id       TEXT NOT NULL,
              meter          TEXT NOT NULL,
              amount         TEXT NOT NULL,
              settled_at     REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_window_events_scope
              ON window_events(scope_id, meter, settled_at);
            """
        )
        self._set_schema_version()
        self._conn.commit()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def put(self, node: Node) -> None:
        _validate_for_put(node)
        payload, blobs, literal_paths = _intern_payload(
            node.payload,
            enabled=self.intern_payloads,
            threshold=self.intern_threshold,
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if node.parent is not None:
                parent = self._conn.execute(
                    "SELECT 1 FROM nodes WHERE id = ?", (node.parent,)
                ).fetchone()
                if parent is None:
                    raise KeyError(node.parent)
            existing = self._get_optional(node.id)
            if existing is not None:
                self._handle_existing_locked(existing, node)
                self._conn.commit()
                return
            for digest, value in blobs.items():
                row = self._conn.execute(
                    "SELECT value FROM blobs WHERE digest = ?", (digest,)
                ).fetchone()
                if row is not None and str(row[0]) != value:
                    raise IntegrityError(f"blob digest collision for {digest}")
                self._conn.execute(
                    "INSERT OR IGNORE INTO blobs (digest, value) VALUES (?, ?)",
                    (digest, value),
                )
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
                    canonical_bytes(payload).decode("utf-8"),
                    node.result_text,
                    node.result_digest,
                    _json_text(node.meta),
                ),
            )
            self._conn.executemany(
                "INSERT INTO blob_literals (node_id, path) VALUES (?, ?)",
                ((node.id, path) for path in literal_paths),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

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
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT meta FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise KeyError(node_id)
            meta = {**json.loads(str(row[0])), **patch}
            self._conn.execute(
                "UPDATE nodes SET meta = ? WHERE id = ?",
                (_json_text(meta), node_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def walk(self, root_id: str) -> Iterator[Node]:
        pending = [root_id]
        while pending:
            node_id = pending.pop()
            yield self.get(node_id)
            pending.extend(reversed(self.children(node_id)))

    def roots(self) -> list[str]:
        rows = self._conn.execute("SELECT id FROM nodes WHERE parent IS NULL").fetchall()
        root_ids = [str(row[0]) for row in rows]
        return sorted(
            root_ids,
            key=lambda item: (str(self.get(item).payload.get("run", "")), item),
        )

    def _pollard_reserve(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            for request in sorted(budgets, key=lambda item: item.scope_id):
                for meter, limit in sorted(request.limits.items()):
                    if meter == "depth":
                        continue
                    baseline = request.baseline.get(meter, Decimal("0"))
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO budget_state (scope_id, meter, settled)
                        VALUES (?, ?, ?)
                        """,
                        (request.scope_id, meter, str(baseline)),
                    )
                    row = self._conn.execute(
                        """
                        SELECT settled FROM budget_state
                        WHERE scope_id = ? AND meter = ?
                        """,
                        (request.scope_id, meter),
                    ).fetchone()
                    if row is None:
                        raise IntegrityError("budget state disappeared during reservation")
                    settled = Decimal(str(row[0]))
                    if baseline > settled:
                        settled = baseline
                        self._conn.execute(
                            """
                            UPDATE budget_state SET settled = ?
                            WHERE scope_id = ? AND meter = ?
                            """,
                            (str(settled), request.scope_id, meter),
                        )
                    active = _decimal_sum(
                        row[0]
                        for row in self._conn.execute(
                            """
                            SELECT amount FROM reservations
                            WHERE kind = 'budget' AND scope_id = ? AND meter = ?
                              AND expires_at > ?
                            """,
                            (request.scope_id, meter, now),
                        ).fetchall()
                    )
                    amount = request.estimates.get(meter, Decimal("0"))
                    remaining = limit - settled - active
                    if amount > remaining:
                        self._conn.commit()
                        return ReservationCheck(
                            ok=False,
                            meter=meter,
                            requested=amount,
                            remaining=remaining,
                        )
            for window_request in sorted(windows, key=lambda item: item.ledger_key):
                cutoff = now - window_request.window_seconds
                self._conn.execute(
                    "DELETE FROM window_events WHERE scope_id = ? AND settled_at <= ?",
                    (window_request.ledger_key, cutoff),
                )
                settled = _decimal_sum(
                    row[0]
                    for row in self._conn.execute(
                        """
                        SELECT amount FROM window_events
                        WHERE scope_id = ? AND settled_at > ?
                        """,
                        (window_request.ledger_key, cutoff),
                    ).fetchall()
                )
                active = _decimal_sum(
                    row[0]
                    for row in self._conn.execute(
                        """
                        SELECT amount FROM reservations
                        WHERE kind = 'window' AND scope_id = ? AND expires_at > ?
                        """,
                        (window_request.ledger_key, now),
                    ).fetchall()
                )
                remaining = window_request.limit - settled - active
                if window_request.amount > remaining:
                    self._conn.commit()
                    return ReservationCheck(
                        ok=False,
                        reason="window",
                        meter=window_request.meter,
                        requested=window_request.amount,
                        remaining=remaining,
                        window_seconds=window_request.window_seconds,
                    )
            expires_at = (
                float("inf") if self.path == Path(":memory:") else now + lease_seconds
            )
            for request in budgets:
                for meter in sorted(request.limits):
                    if meter == "depth":
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO reservations
                          (reservation_id, kind, scope_id, meter, amount,
                           expires_at, window_seconds)
                        VALUES (?, 'budget', ?, ?, ?, ?, NULL)
                        """,
                        (
                            reservation_id,
                            request.scope_id,
                            meter,
                            str(request.estimates.get(meter, Decimal("0"))),
                            expires_at,
                        ),
                    )
            for window_request in windows:
                self._conn.execute(
                    """
                    INSERT INTO reservations
                      (reservation_id, kind, scope_id, meter, amount,
                       expires_at, window_seconds)
                    VALUES (?, 'window', ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        window_request.ledger_key,
                        window_request.meter,
                        str(window_request.amount),
                        expires_at,
                        window_request.window_seconds,
                    ),
                )
            self._conn.commit()
            return ReservationCheck(ok=True)
        except BaseException:
            self._conn.rollback()
            raise

    def _pollard_settle(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            rows = self._conn.execute(
                """
                SELECT kind, scope_id, meter, window_seconds
                FROM reservations WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchall()
            for row in rows:
                kind, scope_id, meter = str(row[0]), str(row[1]), str(row[2])
                actual = charges.get(meter, Decimal("0"))
                if kind == "budget":
                    state = self._conn.execute(
                        """
                        SELECT settled FROM budget_state
                        WHERE scope_id = ? AND meter = ?
                        """,
                        (scope_id, meter),
                    ).fetchone()
                    if state is None:
                        raise IntegrityError("budget state missing during settlement")
                    settled = Decimal(str(state[0])) + actual
                    self._conn.execute(
                        """
                        UPDATE budget_state SET settled = ?
                        WHERE scope_id = ? AND meter = ?
                        """,
                        (str(settled), scope_id, meter),
                    )
                elif kind == "window" and actual != 0:
                    self._conn.execute(
                        """
                        INSERT INTO window_events
                          (scope_id, meter, amount, settled_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (scope_id, meter, str(actual), now),
                    )
            self._conn.execute(
                "DELETE FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _pollard_release(self, reservation_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            )

    def _pollard_renew(self, reservation_id: str, lease_seconds: float) -> bool:
        if self.path == Path(":memory:"):
            return True
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = conn.execute(
                """
                SELECT COUNT(*), MIN(expires_at)
                FROM reservations WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            if (
                row is None
                or int(row[0]) == 0
                or row[1] is None
                or float(row[1]) <= now
            ):
                conn.commit()
                return False
            conn.execute(
                "UPDATE reservations SET expires_at = ? WHERE reservation_id = ?",
                (now + lease_seconds, reservation_id),
            )
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _pollard_drop_nodes(self, node_ids: set[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "DELETE FROM blob_literals WHERE node_id = ?",
                ((node_id,) for node_id in node_ids),
            )
            self._conn.executemany(
                "DELETE FROM nodes WHERE id = ?",
                ((node_id,) for node_id in node_ids),
            )

    def _pollard_compact(self) -> int:
        referenced: set[str] = set()
        rows = self._conn.execute("SELECT id, payload FROM nodes").fetchall()
        for row in rows:
            node_id = str(row[0])
            value = json.loads(str(row[1]))
            literals = self._literal_paths(node_id)
            referenced.update(_referenced_blobs(value, (), literals))
        stored = {
            str(row[0]) for row in self._conn.execute("SELECT digest FROM blobs").fetchall()
        }
        unused = stored - referenced
        with self._conn:
            self._conn.executemany(
                "DELETE FROM blobs WHERE digest = ?",
                ((digest,) for digest in unused),
            )
            self._conn.execute(
                "DELETE FROM reservations WHERE expires_at <= ?", (time.time(),)
            )
        self._conn.execute("VACUUM")
        return len(unused)

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
        payload = _rehydrate_payload(
            json.loads(str(row[4])),
            (),
            self._literal_paths(str(row[0])),
            self._blob_value,
        )
        return Node.from_storage(
            id=str(row[0]),
            parent=None if row[1] is None else str(row[1]),
            kind=str(row[2]),
            attempt=int(row[3]),
            payload_text=canonical_bytes(payload).decode("utf-8"),
            result_text=None if row[5] is None else str(row[5]),
            result_digest=None if row[6] is None else str(row[6]),
            meta_text=str(row[7]),
        )

    def _handle_existing_locked(self, existing: Node, incoming: Node) -> None:
        if existing.identity_tuple() != incoming.identity_tuple():
            raise IntegrityError(f"node id collision for {incoming.id}")
        if incoming.result_text is None or incoming.result_text == existing.result_text:
            return
        conflicts = list(existing.meta.get("result_conflicts", []))
        conflicts.append({"result_digest": incoming.result_digest, "result": incoming.result})
        meta = {**existing.meta, "result_conflicts": conflicts}
        self._conn.execute(
            "UPDATE nodes SET meta = ? WHERE id = ?",
            (_json_text(meta), existing.id),
        )

    def _blob_value(self, digest: str) -> str:
        row = self._conn.execute(
            "SELECT value FROM blobs WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"missing interned payload blob: {digest}")
        return str(row[0])

    def _literal_paths(self, node_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT path FROM blob_literals WHERE node_id = ?", (node_id,)
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _set_schema_version(self) -> None:
        row = self._conn.execute(
            "SELECT v FROM kv WHERE k = ?", ("schema_version",)
        ).fetchone()
        if row is not None and int(row[0]) > 3:
            raise IntegrityError(f"unsupported SQLite schema version: {row[0]}")
        version = 0 if row is None else int(row[0])
        if version < 2:
            self._migrate_blob_literals()
        if version < 3:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)",
                ("schema_version", "3"),
            )

    def _migrate_blob_literals(self) -> None:
        rows = self._conn.execute("SELECT id, payload FROM nodes").fetchall()
        for row in rows:
            node_id = str(row[0])
            payload = json.loads(str(row[1]))
            self._conn.executemany(
                "INSERT OR IGNORE INTO blob_literals (node_id, path) VALUES (?, ?)",
                ((node_id, path) for path in _literal_blob_paths(payload, ())),
            )


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decimal_sum(values: Iterator[object]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal("0"))


_BLOB_REF = "__pollard_ref"


def _intern_payload(
    payload: dict[str, IdentityValue],
    *,
    enabled: bool,
    threshold: int,
) -> tuple[dict[str, IdentityValue], dict[str, str], list[str]]:
    blobs: dict[str, str] = {}
    literal_paths: list[str] = []

    def visit(value: IdentityValue, path: tuple[str | int, ...]) -> IdentityValue:
        if _blob_digest(value) is not None:
            literal_paths.append(_path_text(path))
            return value
        if enabled and isinstance(value, str) and len(value.encode("utf-8")) >= threshold:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            blobs[digest] = value
            return {_BLOB_REF: digest}
        if isinstance(value, dict):
            return {key: visit(item, (*path, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, (*path, index)) for index, item in enumerate(value)]
        return value

    stored = visit(payload, ())
    if not isinstance(stored, dict):
        raise TypeError("node payload must be an object")
    return stored, blobs, literal_paths


def _rehydrate_payload(
    value: IdentityValue,
    path: tuple[str | int, ...],
    literal_paths: set[str],
    blob_value: object,
) -> IdentityValue:
    digest = _blob_digest(value)
    if digest is not None and _path_text(path) not in literal_paths:
        if not callable(blob_value):
            raise TypeError("blob_value must be callable")
        result = blob_value(digest)
        if not isinstance(result, str):
            raise TypeError("interned blob must be a string")
        return result
    if isinstance(value, dict):
        return {
            key: _rehydrate_payload(item, (*path, key), literal_paths, blob_value)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _rehydrate_payload(item, (*path, index), literal_paths, blob_value)
            for index, item in enumerate(value)
        ]
    return value


def _referenced_blobs(
    value: object,
    path: tuple[str | int, ...],
    literal_paths: set[str],
) -> set[str]:
    digest = _blob_digest(value)
    if digest is not None:
        return set() if _path_text(path) in literal_paths else {digest}
    referenced: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            referenced.update(_referenced_blobs(item, (*path, str(key)), literal_paths))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            referenced.update(_referenced_blobs(item, (*path, index), literal_paths))
    return referenced


def _literal_blob_paths(
    value: object,
    path: tuple[str | int, ...],
) -> list[str]:
    if _blob_digest(value) is not None:
        return [_path_text(path)]
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            paths.extend(_literal_blob_paths(item, (*path, str(key))))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_literal_blob_paths(item, (*path, index)))
    return paths


def _blob_digest(value: object) -> str | None:
    if not isinstance(value, dict) or set(value) != {_BLOB_REF}:
        return None
    digest = value.get(_BLOB_REF)
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    if any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _path_text(path: tuple[str | int, ...]) -> str:
    return json.dumps(path, separators=(",", ":"), ensure_ascii=False)
