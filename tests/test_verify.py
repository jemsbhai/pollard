import sqlite3
from pathlib import Path

from pollard import SQLiteStore, verify
from pollard.tree import Node, NodeKind


def test_verify_clean_tree_passes(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "store.db") as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
        child = Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock-1"},
            result={"text": "ok"},
        )
        store.put(root)
        store.put(child)
        report = verify(store, child.id)
    assert report.ok
    assert report.findings == []


def test_verify_detects_tampered_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    with SQLiteStore(db_path) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
        store.put(root)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE nodes SET payload = ? WHERE id = ?", ('{"run":"tampered"}', root.id))
    conn.commit()
    conn.close()

    with SQLiteStore(db_path) as store:
        report = verify(store, root.id)
    assert not report.ok
    assert report.findings[0].message == "node id does not match identity fields"


def test_verify_detects_tampered_result_text(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    with SQLiteStore(db_path) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
        child = Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock-1"},
            result={"text": "ok"},
        )
        store.put(root)
        store.put(child)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE nodes SET result = ? WHERE id = ?", ('{"text":"changed"}', child.id))
    conn.commit()
    conn.close()

    with SQLiteStore(db_path) as store:
        report = verify(store, child.id)
    assert not report.ok
    assert report.findings[0].message == "result digest does not match stored result"
