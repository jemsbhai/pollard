"""PostgreSQL-backed store for shared, multi-writer runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import suppress
from decimal import Decimal
from importlib import import_module
from typing import Any

from pollard._canon import IdentityValue, canonical_bytes
from pollard.arbiter import (
    BudgetReservation,
    ReservationCheck,
    WindowReservation,
)
from pollard.errors import IntegrityError, ReservationUncertain, SettlementUncertain
from pollard.store import _validate_for_put
from pollard.tree import Node

from .sqlite import (
    _intern_payload,
    _json_text,
    _referenced_blobs,
    _rehydrate_payload,
)

POSTGRES_SCHEMA_VERSION = 2
_SCHEMA_LOCK_NAME = "pollard-schema"
_LEGACY_TABLES = (
    "pollard_nodes",
    "pollard_blobs",
    "pollard_blob_literals",
    "pollard_budget_state",
    "pollard_reservations",
    "pollard_window_scopes",
    "pollard_window_events",
)
_POLLARD_TABLES = (*_LEGACY_TABLES, "pollard_reservation_state")
_LEGACY_COLUMNS = {
    "pollard_nodes": {
        "store_id",
        "id",
        "parent",
        "kind",
        "attempt",
        "payload",
        "result",
        "result_digest",
        "meta",
    },
    "pollard_blobs": {"store_id", "digest", "value"},
    "pollard_blob_literals": {"store_id", "node_id", "path"},
    "pollard_budget_state": {"store_id", "scope_id", "meter", "settled"},
    "pollard_reservations": {
        "store_id",
        "reservation_id",
        "kind",
        "scope_id",
        "meter",
        "amount",
        "expires_at",
        "window_seconds",
    },
    "pollard_window_scopes": {"store_id", "ledger_key"},
    "pollard_window_events": {
        "event_id",
        "store_id",
        "scope_id",
        "meter",
        "amount",
        "settled_at",
    },
}
_RESERVATION_STATE_COLUMNS = {
    "store_id",
    "reservation_id",
    "request_digest",
    "request",
    "state",
    "charges_digest",
    "charges",
    "expires_at",
    "created_at",
    "completed_at",
}
_RESERVATION_STATE_TABLE = """
    CREATE TABLE pollard_reservation_state (
      store_id       TEXT NOT NULL,
      reservation_id TEXT NOT NULL,
      request_digest TEXT NOT NULL,
      request        TEXT NOT NULL,
      state          TEXT NOT NULL CHECK (state IN ('active', 'settled', 'released')),
      charges_digest TEXT,
      charges        TEXT,
      expires_at     DOUBLE PRECISION NOT NULL,
      created_at     DOUBLE PRECISION NOT NULL,
      completed_at   DOUBLE PRECISION,
      PRIMARY KEY (store_id, reservation_id)
    )
"""
_CURRENT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE pollard_nodes (
      store_id TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, kind TEXT NOT NULL,
      attempt INTEGER NOT NULL, payload TEXT NOT NULL, result TEXT,
      result_digest TEXT, meta TEXT NOT NULL, PRIMARY KEY (store_id, id)
    )
    """,
    "CREATE INDEX pollard_nodes_parent_idx ON pollard_nodes (store_id, parent)",
    """
    CREATE TABLE pollard_blobs (
      store_id TEXT NOT NULL, digest TEXT NOT NULL, value TEXT NOT NULL,
      PRIMARY KEY (store_id, digest)
    )
    """,
    """
    CREATE TABLE pollard_blob_literals (
      store_id TEXT NOT NULL, node_id TEXT NOT NULL, path TEXT NOT NULL,
      PRIMARY KEY (store_id, node_id, path)
    )
    """,
    """
    CREATE TABLE pollard_budget_state (
      store_id TEXT NOT NULL, scope_id TEXT NOT NULL, meter TEXT NOT NULL,
      settled NUMERIC NOT NULL, PRIMARY KEY (store_id, scope_id, meter)
    )
    """,
    """
    CREATE TABLE pollard_reservations (
      store_id TEXT NOT NULL, reservation_id TEXT NOT NULL, kind TEXT NOT NULL,
      scope_id TEXT NOT NULL, meter TEXT NOT NULL, amount NUMERIC NOT NULL,
      expires_at DOUBLE PRECISION NOT NULL, window_seconds DOUBLE PRECISION,
      PRIMARY KEY (store_id, reservation_id, kind, scope_id, meter)
    )
    """,
    """
    CREATE INDEX pollard_reservations_scope_idx
    ON pollard_reservations (store_id, kind, scope_id, meter, expires_at)
    """,
    """
    CREATE TABLE pollard_window_scopes (
      store_id TEXT NOT NULL, ledger_key TEXT NOT NULL,
      PRIMARY KEY (store_id, ledger_key)
    )
    """,
    """
    CREATE TABLE pollard_window_events (
      event_id BIGSERIAL PRIMARY KEY, store_id TEXT NOT NULL,
      scope_id TEXT NOT NULL, meter TEXT NOT NULL, amount NUMERIC NOT NULL,
      settled_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX pollard_window_events_scope_idx
    ON pollard_window_events (store_id, scope_id, meter, settled_at)
    """,
    _RESERVATION_STATE_TABLE,
    """
    CREATE TABLE pollard_schema (
      singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
      version INTEGER NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    f"INSERT INTO pollard_schema (singleton, version) VALUES (1, {POSTGRES_SCHEMA_VERSION})",
)


class PostgresStore:
    """A logical Pollard store in PostgreSQL, isolated by ``store_id``."""

    def __init__(
        self,
        conninfo: str,
        *,
        store_id: str = "default",
        intern_payloads: bool = True,
        intern_threshold: int = 1024,
    ) -> None:
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be a non-empty string")
        if isinstance(intern_threshold, bool) or intern_threshold < 1:
            raise ValueError("intern_threshold must be a positive integer")
        try:
            psycopg = import_module("psycopg")
        except ImportError as exc:
            raise ImportError(
                "PostgresStore requires the 'pg' extra: pip install 'pollard[pg]'"
            ) from exc
        self.conninfo = conninfo
        self.store_id = store_id
        self.intern_payloads = intern_payloads
        self.intern_threshold = intern_threshold
        self._psycopg = psycopg
        self._conn: Any = self._connect()
        try:
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    @classmethod
    def migrate(cls, conninfo: str) -> tuple[int, int]:
        """Migrate a drained legacy PostgreSQL schema to the current version."""

        try:
            psycopg = import_module("psycopg")
        except ImportError as exc:
            raise ImportError(
                "PostgresStore requires the 'pg' extra: pip install 'pollard[pg]'"
            ) from exc
        with psycopg.connect(conninfo, autocommit=True) as conn:
            with conn.transaction():
                cls._lock_schema(conn)
                version = cls._read_schema_version(conn)
                original = 0 if version is None else version
                if version is None:
                    existing = cls._existing_pollard_tables(conn)
                    if not existing:
                        cls._create_current_schema(conn)
                        return (0, POSTGRES_SCHEMA_VERSION)
                    if existing != set(_LEGACY_TABLES):
                        names = ", ".join(sorted(existing))
                        raise IntegrityError(
                            "unsupported unversioned PostgreSQL schema; "
                            f"found Pollard tables: {names or 'none'}"
                        )
                    cls._require_table_layout(conn, _LEGACY_COLUMNS)
                    cls._refuse_live_migration(conn)
                    conn.execute(
                        """
                        CREATE TABLE pollard_schema (
                          singleton  INTEGER PRIMARY KEY CHECK (singleton = 1),
                          version    INTEGER NOT NULL,
                          updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    conn.execute(
                        "INSERT INTO pollard_schema (singleton, version) VALUES (1, 1)"
                    )
                    version = 1
                if version > POSTGRES_SCHEMA_VERSION or version < 1:
                    raise IntegrityError(
                        f"unsupported PostgreSQL schema version: {version}"
                    )
                if version == POSTGRES_SCHEMA_VERSION:
                    cls._require_current_schema(conn)
                if version < 2:
                    cls._require_table_layout(
                        conn,
                        {
                            **_LEGACY_COLUMNS,
                            "pollard_schema": {"singleton", "version", "updated_at"},
                        },
                    )
                    cls._refuse_live_migration(conn)
                    conn.execute(_RESERVATION_STATE_TABLE)
                    conn.execute(
                        """
                        UPDATE pollard_schema
                        SET version = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE singleton = 1
                        """,
                        (2,),
                    )
            return (original, POSTGRES_SCHEMA_VERSION)

    def __enter__(self) -> PostgresStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def reconnect(self) -> None:
        """Replace a broken connection and refuse any incompatible schema."""

        with suppress(Exception):
            self._conn.close()
        self._conn = self._connect()
        try:
            with self._conn.transaction():
                self._lock_schema(self._conn)
                self._require_current_schema(self._conn)
        except BaseException:
            self._conn.close()
            raise

    def _connect(self) -> Any:
        return self._psycopg.connect(self.conninfo, autocommit=True)

    def put(self, node: Node) -> None:
        _validate_for_put(node)
        payload, blobs, literal_paths = _intern_payload(
            node.payload,
            enabled=self.intern_payloads,
            threshold=self.intern_threshold,
        )
        with self._conn.transaction():
            if node.parent is not None:
                parent = self._conn.execute(
                    "SELECT 1 FROM pollard_nodes WHERE store_id = %s AND id = %s",
                    (self.store_id, node.parent),
                ).fetchone()
                if parent is None:
                    raise KeyError(node.parent)
            for digest, value in blobs.items():
                inserted = self._conn.execute(
                    """
                    INSERT INTO pollard_blobs (store_id, digest, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (store_id, digest) DO NOTHING
                    RETURNING digest
                    """,
                    (self.store_id, digest, value),
                ).fetchone()
                if inserted is None:
                    row = self._conn.execute(
                        """
                        SELECT value FROM pollard_blobs
                        WHERE store_id = %s AND digest = %s
                        """,
                        (self.store_id, digest),
                    ).fetchone()
                    if row is None or str(row[0]) != value:
                        raise IntegrityError(f"blob digest collision for {digest}")
            inserted = self._conn.execute(
                """
                INSERT INTO pollard_nodes
                  (store_id, id, parent, kind, attempt, payload, result, result_digest, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (store_id, id) DO NOTHING
                RETURNING id
                """,
                (
                    self.store_id,
                    node.id,
                    node.parent,
                    node.kind,
                    node.attempt,
                    canonical_bytes(payload).decode("utf-8"),
                    node.result_text,
                    node.result_digest,
                    _json_text(node.meta),
                ),
            ).fetchone()
            if inserted is None:
                existing = self._get_optional(node.id, lock=True)
                if existing is None:
                    raise IntegrityError(f"lost node during concurrent put: {node.id}")
                self._handle_existing_locked(existing, node)
                return
            if literal_paths:
                with self._conn.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO pollard_blob_literals (store_id, node_id, path)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (store_id, node_id, path) DO NOTHING
                        """,
                        ((self.store_id, node.id, path) for path in literal_paths),
                    )

    def get(self, node_id: str) -> Node:
        node = self._get_optional(node_id)
        if node is None:
            raise KeyError(node_id)
        return node

    def exists(self, node_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pollard_nodes WHERE store_id = %s AND id = %s",
            (self.store_id, node_id),
        ).fetchone()
        return row is not None

    def children(self, node_id: str) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT id FROM pollard_nodes
            WHERE store_id = %s AND parent = %s
            ORDER BY kind ASC, id ASC
            """,
            (self.store_id, node_id),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        with self._conn.transaction():
            row = self._conn.execute(
                """
                SELECT meta FROM pollard_nodes
                WHERE store_id = %s AND id = %s
                FOR UPDATE
                """,
                (self.store_id, node_id),
            ).fetchone()
            if row is None:
                raise KeyError(node_id)
            meta = {**json.loads(str(row[0])), **patch}
            self._conn.execute(
                """
                UPDATE pollard_nodes SET meta = %s
                WHERE store_id = %s AND id = %s
                """,
                (_json_text(meta), self.store_id, node_id),
            )

    def walk(self, root_id: str) -> Iterator[Node]:
        pending = [root_id]
        while pending:
            node_id = pending.pop()
            yield self.get(node_id)
            pending.extend(reversed(self.children(node_id)))

    def roots(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM pollard_nodes WHERE store_id = %s AND parent IS NULL",
            (self.store_id,),
        ).fetchall()
        roots = [str(row[0]) for row in rows]
        return sorted(
            roots,
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
            return self._pollard_reserve_once(
                reservation_id, budgets, windows, lease_seconds
            )
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            return self._pollard_reserve_once(
                reservation_id, budgets, windows, lease_seconds
            )
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise ReservationUncertain(
                "PostgreSQL reservation outcome is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _pollard_reserve_once(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        with self._conn.transaction():
            request_text, request_digest = _reservation_request(
                budgets, windows, lease_seconds
            )
            existing = self._conn.execute(
                """
                SELECT request_digest, state, expires_at
                FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_digest:
                    raise IntegrityError(
                        f"reservation retry changed request: {reservation_id}"
                    )
                state = str(existing[1])
                if state != "active":
                    raise IntegrityError(
                        f"reservation is already {state}: {reservation_id}"
                    )
                now = self._database_time()
                if float(existing[2]) <= now:
                    raise IntegrityError(
                        f"reservation expired before retry: {reservation_id}"
                    )
                return ReservationCheck(ok=True)
            budget_rows = [
                (request, meter, limit)
                for request in budgets
                for meter, limit in request.limits.items()
                if meter != "depth"
            ]
            for request, meter, _limit in sorted(
                budget_rows, key=lambda item: (item[0].scope_id, item[1])
            ):
                baseline = request.baseline.get(meter, Decimal("0"))
                self._conn.execute(
                    """
                    INSERT INTO pollard_budget_state
                      (store_id, scope_id, meter, settled)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (store_id, scope_id, meter) DO NOTHING
                    """,
                    (self.store_id, request.scope_id, meter, str(baseline)),
                )
            for request, meter, _limit in sorted(
                budget_rows, key=lambda item: (item[0].scope_id, item[1])
            ):
                row = self._conn.execute(
                    """
                    SELECT settled FROM pollard_budget_state
                    WHERE store_id = %s AND scope_id = %s AND meter = %s
                    FOR UPDATE
                    """,
                    (self.store_id, request.scope_id, meter),
                ).fetchone()
                if row is None:
                    raise IntegrityError("budget state disappeared during reservation")
            for window_request in sorted(windows, key=lambda item: item.ledger_key):
                self._conn.execute(
                    """
                    INSERT INTO pollard_window_scopes (store_id, ledger_key)
                    VALUES (%s, %s)
                    ON CONFLICT (store_id, ledger_key) DO NOTHING
                    """,
                    (self.store_id, window_request.ledger_key),
                )
                self._conn.execute(
                    """
                    SELECT ledger_key FROM pollard_window_scopes
                    WHERE store_id = %s AND ledger_key = %s
                    FOR UPDATE
                    """,
                    (self.store_id, window_request.ledger_key),
                ).fetchone()
            now = self._database_time()
            for request, meter, limit in sorted(
                budget_rows, key=lambda item: (item[0].scope_id, item[1])
            ):
                row = self._conn.execute(
                    """
                    SELECT settled FROM pollard_budget_state
                    WHERE store_id = %s AND scope_id = %s AND meter = %s
                    """,
                    (self.store_id, request.scope_id, meter),
                ).fetchone()
                if row is None:
                    raise IntegrityError("budget state disappeared during reservation")
                settled = Decimal(str(row[0]))
                baseline = request.baseline.get(meter, Decimal("0"))
                if baseline > settled:
                    settled = baseline
                    self._conn.execute(
                        """
                        UPDATE pollard_budget_state SET settled = %s
                        WHERE store_id = %s AND scope_id = %s AND meter = %s
                        """,
                        (str(settled), self.store_id, request.scope_id, meter),
                    )
                active = _decimal_sum(
                    active_row[0]
                    for active_row in self._conn.execute(
                        """
                        SELECT amount FROM pollard_reservations
                        WHERE store_id = %s AND kind = 'budget'
                          AND scope_id = %s AND meter = %s
                          AND expires_at > %s
                        """,
                        (self.store_id, request.scope_id, meter, now),
                    ).fetchall()
                )
                amount = request.estimates.get(meter, Decimal("0"))
                remaining = limit - settled - active
                if amount > remaining:
                    return ReservationCheck(
                        ok=False,
                        meter=meter,
                        requested=amount,
                        remaining=remaining,
                    )
            for window_request in sorted(windows, key=lambda item: item.ledger_key):
                cutoff = now - window_request.window_seconds
                self._conn.execute(
                    """
                    DELETE FROM pollard_window_events
                    WHERE store_id = %s AND scope_id = %s AND settled_at <= %s
                    """,
                    (self.store_id, window_request.ledger_key, cutoff),
                )
                settled = _decimal_sum(
                    event[0]
                    for event in self._conn.execute(
                        """
                        SELECT amount FROM pollard_window_events
                        WHERE store_id = %s AND scope_id = %s AND settled_at > %s
                        """,
                        (self.store_id, window_request.ledger_key, cutoff),
                    ).fetchall()
                )
                active = _decimal_sum(
                    active_row[0]
                    for active_row in self._conn.execute(
                        """
                        SELECT amount FROM pollard_reservations
                        WHERE store_id = %s AND kind = 'window' AND scope_id = %s
                          AND expires_at > %s
                        """,
                        (self.store_id, window_request.ledger_key, now),
                    ).fetchall()
                )
                remaining = window_request.limit - settled - active
                if window_request.amount > remaining:
                    return ReservationCheck(
                        ok=False,
                        reason="window",
                        meter=window_request.meter,
                        requested=window_request.amount,
                        remaining=remaining,
                        window_seconds=window_request.window_seconds,
                    )
            expires_at = now + lease_seconds
            self._conn.execute(
                """
                INSERT INTO pollard_reservation_state
                  (store_id, reservation_id, request_digest, request, state,
                   charges_digest, charges, expires_at, created_at, completed_at)
                VALUES (%s, %s, %s, %s, 'active', NULL, NULL, %s, %s, NULL)
                """,
                (
                    self.store_id,
                    reservation_id,
                    request_digest,
                    request_text,
                    expires_at,
                    now,
                ),
            )
            for request, meter, _limit in budget_rows:
                self._conn.execute(
                    """
                    INSERT INTO pollard_reservations
                      (store_id, reservation_id, kind, scope_id, meter, amount,
                       expires_at, window_seconds)
                    VALUES (%s, %s, 'budget', %s, %s, %s, %s, NULL)
                    """,
                    (
                        self.store_id,
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
                    INSERT INTO pollard_reservations
                      (store_id, reservation_id, kind, scope_id, meter, amount,
                       expires_at, window_seconds)
                    VALUES (%s, %s, 'window', %s, %s, %s, %s, %s)
                    """,
                    (
                        self.store_id,
                        reservation_id,
                        window_request.ledger_key,
                        window_request.meter,
                        str(window_request.amount),
                        expires_at,
                        window_request.window_seconds,
                    ),
                )
            return ReservationCheck(ok=True)

    def _pollard_settle(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        try:
            self._pollard_settle_once(reservation_id, charges)
            return
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            self._pollard_settle_once(reservation_id, charges)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise SettlementUncertain(
                "PostgreSQL settlement outcome is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _pollard_settle_once(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        with self._conn.transaction():
            charges_text, charges_digest = _reservation_charges(charges)
            state_row = self._conn.execute(
                """
                SELECT state, charges_digest
                FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchone()
            if state_row is None:
                raise IntegrityError(f"unknown reservation: {reservation_id}")
            state = str(state_row[0])
            if state == "settled":
                if str(state_row[1]) != charges_digest:
                    raise IntegrityError(
                        f"reservation retry used different charges: {reservation_id}"
                    )
                return
            if state != "active":
                raise IntegrityError(f"reservation is already {state}: {reservation_id}")
            rows = self._conn.execute(
                """
                SELECT kind, scope_id, meter FROM pollard_reservations
                WHERE store_id = %s AND reservation_id = %s
                ORDER BY kind, scope_id, meter
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchall()
            if not rows:
                raise IntegrityError(
                    f"reservation details are missing: {reservation_id}"
                )
            window_scopes = sorted(
                {str(row[1]) for row in rows if str(row[0]) == "window"}
            )
            for scope_id in window_scopes:
                self._conn.execute(
                    """
                    SELECT ledger_key FROM pollard_window_scopes
                    WHERE store_id = %s AND ledger_key = %s
                    FOR UPDATE
                    """,
                    (self.store_id, scope_id),
                ).fetchone()
            now = self._database_time()
            for row in rows:
                kind, scope_id, meter = str(row[0]), str(row[1]), str(row[2])
                actual = charges.get(meter, Decimal("0"))
                if kind == "budget":
                    self._conn.execute(
                        """
                        UPDATE pollard_budget_state
                        SET settled = settled + %s
                        WHERE store_id = %s AND scope_id = %s AND meter = %s
                        """,
                        (str(actual), self.store_id, scope_id, meter),
                    )
                elif kind == "window" and actual != 0:
                    self._conn.execute(
                        """
                        INSERT INTO pollard_window_events
                          (store_id, scope_id, meter, amount, settled_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (self.store_id, scope_id, meter, str(actual), now),
                    )
            self._conn.execute(
                """
                DELETE FROM pollard_reservations
                WHERE store_id = %s AND reservation_id = %s
                """,
                (self.store_id, reservation_id),
            )
            self._conn.execute(
                """
                UPDATE pollard_reservation_state
                SET state = 'settled', charges_digest = %s, charges = %s,
                    completed_at = %s
                WHERE store_id = %s AND reservation_id = %s
                """,
                (
                    charges_digest,
                    charges_text,
                    now,
                    self.store_id,
                    reservation_id,
                ),
            )

    def _pollard_release(self, reservation_id: str) -> None:
        try:
            self._pollard_release_once(reservation_id)
            return
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            self._pollard_release_once(reservation_id)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise ReservationUncertain(
                "PostgreSQL reservation release is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _pollard_release_once(self, reservation_id: str) -> None:
        with self._conn.transaction():
            state_row = self._conn.execute(
                """
                SELECT state FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchone()
            if state_row is None or str(state_row[0]) == "released":
                return
            if str(state_row[0]) != "active":
                raise IntegrityError(
                    f"reservation is already {state_row[0]}: {reservation_id}"
                )
            now = self._database_time()
            self._conn.execute(
                """
                DELETE FROM pollard_reservations
                WHERE store_id = %s AND reservation_id = %s
                """,
                (self.store_id, reservation_id),
            )
            self._conn.execute(
                """
                UPDATE pollard_reservation_state
                SET state = 'released', completed_at = %s
                WHERE store_id = %s AND reservation_id = %s
                """,
                (now, self.store_id, reservation_id),
            )

    def _pollard_renew(self, reservation_id: str, lease_seconds: float) -> bool:
        with (
            self._psycopg.connect(self.conninfo, autocommit=True) as conn,
            conn.transaction(),
        ):
            row = conn.execute(
                """
                SELECT state, expires_at FROM pollard_reservation_state
                WHERE store_id = %s AND reservation_id = %s
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchone()
            now = self._database_time_for(conn)
            if (
                row is None
                or str(row[0]) != "active"
                or float(row[1]) <= now
            ):
                return False
            expires_at = now + lease_seconds
            conn.execute(
                """
                UPDATE pollard_reservation_state SET expires_at = %s
                WHERE store_id = %s AND reservation_id = %s
                """,
                (expires_at, self.store_id, reservation_id),
            )
            conn.execute(
                """
                UPDATE pollard_reservations SET expires_at = %s
                WHERE store_id = %s AND reservation_id = %s
                """,
                (expires_at, self.store_id, reservation_id),
            )
            return True

    def _is_connection_error(self, exc: BaseException) -> bool:
        return isinstance(
            exc,
            (self._psycopg.OperationalError, self._psycopg.InterfaceError),
        )

    def _pollard_drop_nodes(self, node_ids: set[str]) -> None:
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                """
                    DELETE FROM pollard_blob_literals
                    WHERE store_id = %s AND node_id = %s
                    """,
                ((self.store_id, node_id) for node_id in node_ids),
            )
            cursor.executemany(
                "DELETE FROM pollard_nodes WHERE store_id = %s AND id = %s",
                ((self.store_id, node_id) for node_id in node_ids),
            )

    def _pollard_compact(self) -> int:
        referenced: set[str] = set()
        rows = self._conn.execute(
            "SELECT id, payload FROM pollard_nodes WHERE store_id = %s",
            (self.store_id,),
        ).fetchall()
        for row in rows:
            node_id = str(row[0])
            referenced.update(
                _referenced_blobs(
                    json.loads(str(row[1])), (), self._literal_paths(node_id)
                )
            )
        stored = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT digest FROM pollard_blobs WHERE store_id = %s",
                (self.store_id,),
            ).fetchall()
        }
        unused = stored - referenced
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                "DELETE FROM pollard_blobs WHERE store_id = %s AND digest = %s",
                ((self.store_id, digest) for digest in unused),
            )
            cursor.execute(
                """
                DELETE FROM pollard_reservations
                WHERE store_id = %s AND expires_at <= %s
                """,
                (self.store_id, self._database_time()),
            )
        return len(unused)

    def _get_optional(self, node_id: str, *, lock: bool = False) -> Node | None:
        suffix = " FOR UPDATE" if lock else ""
        row = self._conn.execute(
            """
            SELECT id, parent, kind, attempt, payload, result, result_digest, meta
            FROM pollard_nodes
            WHERE store_id = %s AND id = %s
            """
            + suffix,
            (self.store_id, node_id),
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
        conflicts.append(
            {"result_digest": incoming.result_digest, "result": incoming.result}
        )
        meta = {**existing.meta, "result_conflicts": conflicts}
        self._conn.execute(
            """
            UPDATE pollard_nodes SET meta = %s
            WHERE store_id = %s AND id = %s
            """,
            (_json_text(meta), self.store_id, existing.id),
        )

    def _blob_value(self, digest: str) -> str:
        row = self._conn.execute(
            """
            SELECT value FROM pollard_blobs
            WHERE store_id = %s AND digest = %s
            """,
            (self.store_id, digest),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"missing interned payload blob: {digest}")
        return str(row[0])

    def _literal_paths(self, node_id: str) -> set[str]:
        rows = self._conn.execute(
            """
            SELECT path FROM pollard_blob_literals
            WHERE store_id = %s AND node_id = %s
            """,
            (self.store_id, node_id),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _initialize(self) -> None:
        with self._conn.transaction():
            self._lock_schema(self._conn)
            version = self._read_schema_version(self._conn)
            if version is None:
                existing = self._existing_pollard_tables(self._conn)
                if existing == set(_LEGACY_TABLES):
                    raise IntegrityError(
                        "PostgreSQL schema migration required: run "
                        "PostgresStore.migrate() after backup and worker drain"
                    )
                if existing:
                    names = ", ".join(sorted(existing))
                    raise IntegrityError(
                        "unsupported unversioned PostgreSQL schema; "
                        f"found Pollard tables: {names}"
                    )
                self._create_current_schema(self._conn)
                return
            self._require_current_schema(self._conn)

    @staticmethod
    def _lock_schema(conn: Any) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_SCHEMA_LOCK_NAME,),
        )

    @staticmethod
    def _read_schema_version(conn: Any) -> int | None:
        exists = conn.execute("SELECT to_regclass('pollard_schema')").fetchone()
        if exists is None or exists[0] is None:
            return None
        rows = conn.execute(
            "SELECT singleton, version FROM pollard_schema ORDER BY singleton"
        ).fetchall()
        if len(rows) != 1 or int(rows[0][0]) != 1:
            raise IntegrityError("invalid PostgreSQL schema version record")
        return int(rows[0][1])

    @staticmethod
    def _existing_pollard_tables(conn: Any) -> set[str]:
        return {
            name
            for name in _POLLARD_TABLES
            if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0]
            is not None
        }

    @staticmethod
    def _create_current_schema(conn: Any) -> None:
        for statement in _CURRENT_SCHEMA_STATEMENTS:
            conn.execute(statement)

    @staticmethod
    def _require_current_schema(conn: Any) -> None:
        version = PostgresStore._read_schema_version(conn)
        if version == POSTGRES_SCHEMA_VERSION:
            PostgresStore._require_table_layout(
                conn,
                {
                    **_LEGACY_COLUMNS,
                    "pollard_reservation_state": _RESERVATION_STATE_COLUMNS,
                    "pollard_schema": {"singleton", "version", "updated_at"},
                },
            )
            return
        if version is not None and 1 <= version < POSTGRES_SCHEMA_VERSION:
            raise IntegrityError(
                f"PostgreSQL schema migration required: {version} to "
                f"{POSTGRES_SCHEMA_VERSION}"
            )
        raise IntegrityError(f"unsupported PostgreSQL schema version: {version}")

    @staticmethod
    def _require_table_layout(
        conn: Any,
        expected: dict[str, set[str]],
    ) -> None:
        actual = {
            table_name: {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT attname FROM pg_catalog.pg_attribute
                    WHERE attrelid = to_regclass(%s)
                      AND attnum > 0 AND NOT attisdropped
                    """,
                    (table_name,),
                ).fetchall()
            }
            for table_name in expected
        }
        if actual != expected:
            raise IntegrityError("unsupported PostgreSQL table layout for schema version")

    @staticmethod
    def _refuse_live_migration(conn: Any) -> None:
        row = conn.execute("SELECT COUNT(*) FROM pollard_reservations").fetchone()
        if row is not None and int(row[0]) != 0:
            raise IntegrityError(
                "PostgreSQL migration requires an empty reservation table; "
                "drain workers and resolve in-flight calls first"
            )

    def _database_time(self) -> float:
        return self._database_time_for(self._conn)

    @staticmethod
    def _database_time_for(conn: Any) -> float:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
        ).fetchone()
        if row is None:
            raise IntegrityError("PostgreSQL did not return its current time")
        return float(row[0])


def _decimal_sum(values: Iterator[object]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal("0"))


def _reservation_request(
    budgets: list[BudgetReservation],
    windows: list[WindowReservation],
    lease_seconds: float,
) -> tuple[str, str]:
    document: IdentityValue = {
        "budgets": [
            {
                "scope_id": request.scope_id,
                "limits": {
                    name: str(value) for name, value in sorted(request.limits.items())
                },
                "baseline": {
                    name: str(value)
                    for name, value in sorted(request.baseline.items())
                },
                "estimates": {
                    name: str(value)
                    for name, value in sorted(request.estimates.items())
                },
            }
            for request in sorted(budgets, key=lambda item: item.scope_id)
        ],
        "windows": [
            {
                "ledger_key": request.ledger_key,
                "meter": request.meter,
                "limit": str(request.limit),
                "amount": str(request.amount),
                "window_seconds": str(request.window_seconds),
            }
            for request in sorted(windows, key=lambda item: item.ledger_key)
        ],
        "lease_seconds": str(lease_seconds),
    }
    encoded = canonical_bytes(document)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _reservation_charges(charges: dict[str, Decimal]) -> tuple[str, str]:
    encoded = canonical_bytes(
        {name: str(value) for name, value in sorted(charges.items())}
    )
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()
