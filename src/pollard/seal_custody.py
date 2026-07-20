"""Reference external custody log for Pollard subtree seals."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .seal import SealReport


@dataclass(frozen=True)
class SealCustodyRecord:
    """One append-only publication of a Pollard seal digest."""

    sequence: int
    store_id: str
    root_id: str
    algorithm: str
    digest: str
    sealed_at: str
    signer_identity: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "sequence": self.sequence,
            "store_id": self.store_id,
            "root_id": self.root_id,
            "algorithm": self.algorithm,
            "digest": self.digest,
            "sealed_at": self.sealed_at,
            "signer_identity": self.signer_identity,
        }


class SQLiteSealSink:
    """Append seal custody records to a separate SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes'"
            ).fetchone():
                raise ValueError("seal custody sink must not use a Pollard store database")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seal_custody_schema (
                  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                  version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seal_custody_records (
                  sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
                  store_id        TEXT NOT NULL,
                  root_id         TEXT NOT NULL,
                  algorithm       TEXT NOT NULL,
                  digest          TEXT NOT NULL,
                  sealed_at       TEXT NOT NULL,
                  signer_identity TEXT NOT NULL
                );
                """
            )
            row = conn.execute(
                "SELECT version FROM seal_custody_schema WHERE singleton = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO seal_custody_schema (singleton, version)
                    VALUES (1, 1)
                    """
                )
                row = conn.execute(
                    "SELECT version FROM seal_custody_schema WHERE singleton = 1"
                ).fetchone()
            if row is None or int(row[0]) != 1:
                version = "missing" if row is None else str(row[0])
                raise ValueError(f"unsupported seal custody schema version: {version}")

    def publish(
        self,
        report: SealReport,
        *,
        store_id: str,
        signer_identity: str,
        sealed_at: str | None = None,
    ) -> SealCustodyRecord:
        """Append and durably return one custody record."""

        if not store_id:
            raise ValueError("store_id must be a non-empty string")
        if not signer_identity:
            raise ValueError("signer_identity must be a non-empty string")
        timestamp = sealed_at or _now_utc()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO seal_custody_records
                  (store_id, root_id, algorithm, digest, sealed_at, signer_identity)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    store_id,
                    report.root_id,
                    report.algorithm,
                    report.digest,
                    timestamp,
                    signer_identity,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("seal custody sink did not return a sequence")
            sequence = int(cursor.lastrowid)
            conn.commit()
        return SealCustodyRecord(
            sequence=sequence,
            store_id=store_id,
            root_id=report.root_id,
            algorithm=report.algorithm,
            digest=report.digest,
            sealed_at=timestamp,
            signer_identity=signer_identity,
        )

    def records(self) -> list[SealCustodyRecord]:
        """Return custody records in publication order."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence, store_id, root_id, algorithm, digest,
                       sealed_at, signer_identity
                FROM seal_custody_records ORDER BY sequence
                """
            ).fetchall()
        return [
            SealCustodyRecord(
                sequence=int(row[0]),
                store_id=str(row[1]),
                root_id=str(row[2]),
                algorithm=str(row[3]),
                digest=str(row[4]),
                sealed_at=str(row[5]),
                signer_identity=str(row[6]),
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
