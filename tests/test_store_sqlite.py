import hashlib
import sqlite3
from pathlib import Path

import pytest

from pollard import IntegrityError, SQLiteStore
from pollard._canon import canonical_bytes
from pollard.tree import Node, NodeKind

from .test_store_shared import root_and_children


def test_sqlite_store_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    with SQLiteStore(db_path) as store:
        root, children = root_and_children(store)
        expected_ids = [child.id for child in children]

    with SQLiteStore(db_path) as reopened:
        assert reopened.get(root.id).payload == {"run": "shared"}
        assert reopened.children(root.id) == sorted(
            expected_ids,
            key=lambda item: (reopened.get(item).kind, item),
        )


def test_gitignore_excludes_sqlite_wal_sidecars() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "*.db-wal" in gitignore
    assert "*.db-shm" in gitignore


def test_payload_interning_is_storage_only_and_configurable(tmp_path: Path) -> None:
    text = "x" * 1024
    node = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": text})
    interned_path = tmp_path / "interned.db"
    plain_path = tmp_path / "plain.db"
    with SQLiteStore(interned_path) as interned, SQLiteStore(
        plain_path, intern_payloads=False
    ) as plain:
        interned.put(node)
        plain.put(node)
        assert interned.get(node.id) == plain.get(node.id) == node
        assert canonical_bytes(interned.get(node.id).payload) == canonical_bytes(node.payload)
        row = interned._conn.execute("SELECT payload FROM nodes").fetchone()
        assert "__pollard_ref" in str(row[0])
        assert interned._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert plain._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    with SQLiteStore(
        tmp_path / "threshold.db", intern_threshold=len(text.encode()) + 1
    ) as threshold_store:
        threshold_store.put(node)
        assert threshold_store._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_repeated_large_strings_share_one_blob(tmp_path: Path) -> None:
    text = "repeat" * 300
    with SQLiteStore(tmp_path / "shared.db") as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "shared"})
        first = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"text": text})
        second = Node.make(
            kind=NodeKind.NOTE,
            parent=root.id,
            payload={"nested": [text, {"again": text}]},
            attempt=1,
        )
        for node in (root, first, second):
            store.put(node)
        assert store._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert store.get(first.id).payload == {"text": text}
        assert store.get(second.id).payload == {"nested": [text, {"again": text}]}


def test_caller_blob_ref_shape_round_trips_without_aliasing(tmp_path: Path) -> None:
    text = "z" * 2048
    digest = hashlib.sha256(text.encode()).hexdigest()
    literal = {"__pollard_ref": digest}
    with SQLiteStore(tmp_path / "literal.db") as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": text})
        child = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"literal": literal})
        store.put(root)
        store.put(child)
        assert store.get(child.id).payload == {"literal": literal}
        assert store._conn.execute("SELECT COUNT(*) FROM blob_literals").fetchone()[0] == 1


def test_missing_interned_blob_is_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    text = "m" * 2048
    with SQLiteStore(path) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": text})
        store.put(root)
        store._conn.execute("DELETE FROM blobs")
        store._conn.commit()
        with pytest.raises(IntegrityError, match="missing interned"):
            store.get(root.id)


def test_schema_one_database_migrates_without_changing_nodes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "legacy", "literal": {"__pollard_ref": "0" * 64}},
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE nodes (
              id TEXT PRIMARY KEY, parent TEXT, kind TEXT NOT NULL, attempt INTEGER NOT NULL,
              payload TEXT NOT NULL, result TEXT, result_digest TEXT, meta TEXT NOT NULL
            );
            CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            INSERT INTO kv VALUES ('schema_version', '1');
            """
        )
        conn.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                root.id,
                None,
                root.kind,
                root.attempt,
                canonical_bytes(root.payload).decode(),
                None,
                None,
                "{}",
            ),
        )
    with SQLiteStore(path) as store:
        assert store.get(root.id) == root
        assert store._conn.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()[0] == "2"


def test_reopening_schema_two_database_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "stable.db"
    with SQLiteStore(path) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "stable"})
        store.put(root)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with SQLiteStore(path) as reopened:
        assert reopened.get(root.id) == root
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert after == before
