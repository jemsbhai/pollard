"""PostgreSQL-backed store for shared, multi-writer runs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from importlib import import_module
from typing import Any

from pollard._canon import canonical_bytes
from pollard.arbiter import (
    BudgetReservation,
    ReservationCheck,
    WindowReservation,
)
from pollard.errors import IntegrityError
from pollard.store import _validate_for_put
from pollard.tree import Node

from .sqlite import (
    _intern_payload,
    _json_text,
    _referenced_blobs,
    _rehydrate_payload,
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
        self._conn: Any = psycopg.connect(conninfo, autocommit=True)
        self._initialize()

    def __enter__(self) -> PostgresStore:
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
        with self._conn.transaction():
            now = self._database_time()
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
            for request, meter, limit in sorted(
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
        with self._conn.transaction():
            now = self._database_time()
            rows = self._conn.execute(
                """
                SELECT kind, scope_id, meter FROM pollard_reservations
                WHERE store_id = %s AND reservation_id = %s
                ORDER BY kind, scope_id, meter
                FOR UPDATE
                """,
                (self.store_id, reservation_id),
            ).fetchall()
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

    def _pollard_release(self, reservation_id: str) -> None:
        with self._conn.transaction():
            self._conn.execute(
                """
                DELETE FROM pollard_reservations
                WHERE store_id = %s AND reservation_id = %s
                """,
                (self.store_id, reservation_id),
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
        statements = (
            """
            CREATE TABLE IF NOT EXISTS pollard_nodes (
              store_id      TEXT NOT NULL,
              id            TEXT NOT NULL,
              parent        TEXT,
              kind          TEXT NOT NULL,
              attempt       INTEGER NOT NULL,
              payload       TEXT NOT NULL,
              result        TEXT,
              result_digest TEXT,
              meta          TEXT NOT NULL,
              PRIMARY KEY (store_id, id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS pollard_nodes_parent_idx
            ON pollard_nodes (store_id, parent)
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_blobs (
              store_id TEXT NOT NULL,
              digest   TEXT NOT NULL,
              value    TEXT NOT NULL,
              PRIMARY KEY (store_id, digest)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_blob_literals (
              store_id TEXT NOT NULL,
              node_id  TEXT NOT NULL,
              path     TEXT NOT NULL,
              PRIMARY KEY (store_id, node_id, path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_budget_state (
              store_id TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              meter    TEXT NOT NULL,
              settled  NUMERIC NOT NULL,
              PRIMARY KEY (store_id, scope_id, meter)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_reservations (
              store_id       TEXT NOT NULL,
              reservation_id TEXT NOT NULL,
              kind           TEXT NOT NULL,
              scope_id       TEXT NOT NULL,
              meter          TEXT NOT NULL,
              amount         NUMERIC NOT NULL,
              expires_at     DOUBLE PRECISION NOT NULL,
              window_seconds DOUBLE PRECISION,
              PRIMARY KEY (store_id, reservation_id, kind, scope_id, meter)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS pollard_reservations_scope_idx
            ON pollard_reservations (store_id, kind, scope_id, meter, expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_window_scopes (
              store_id  TEXT NOT NULL,
              ledger_key TEXT NOT NULL,
              PRIMARY KEY (store_id, ledger_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pollard_window_events (
              event_id   BIGSERIAL PRIMARY KEY,
              store_id   TEXT NOT NULL,
              scope_id   TEXT NOT NULL,
              meter      TEXT NOT NULL,
              amount     NUMERIC NOT NULL,
              settled_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS pollard_window_events_scope_idx
            ON pollard_window_events (store_id, scope_id, meter, settled_at)
            """,
        )
        with self._conn.transaction():
            self._conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("pollard-schema-v1",),
            )
            for statement in statements:
                self._conn.execute(statement)

    def _database_time(self) -> float:
        row = self._conn.execute(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
        ).fetchone()
        if row is None:
            raise IntegrityError("PostgreSQL did not return its current time")
        return float(row[0])


def _decimal_sum(values: Iterator[object]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal("0"))
