import sqlite3
from pathlib import Path

import pytest

from pollard import IntegrityError, MemoryStore, SQLiteStore, seal
from pollard.seal import SEAL_ALGORITHM
from pollard.tree import Node, NodeKind


def _sealed_store(result_text: str = "ok") -> tuple[MemoryStore, str]:
    store = MemoryStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "seal"})
    model = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1"},
        result={"text": result_text},
    )
    note = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"branch": "audit"})
    tool = Node.make(
        kind=NodeKind.TOOL_CALL,
        parent=note.id,
        payload={"tool": "lookup", "args": {"id": 1}},
        result={"value": 7},
    )
    store.put(root)
    store.put(model)
    store.put(note)
    store.put(tool)
    return store, root.id


def test_seal_report_is_deterministic_and_serializable() -> None:
    store, root_id = _sealed_store()

    first = seal(store, root_id)
    second = seal(store, root_id)

    assert first == second
    assert first.algorithm == SEAL_ALGORITHM
    assert len(first.entries) == 4
    assert first.entries[0].previous is None
    assert first.digest == first.entries[-1].seal
    assert first.to_dict()["entries"][0]["node_id"] == root_id


def test_seal_changes_when_result_changes() -> None:
    first_store, first_root = _sealed_store("ok")
    second_store, second_root = _sealed_store("changed")

    first = seal(first_store, first_root)
    second = seal(second_store, second_root)

    assert first_root == second_root
    assert first.entries[1].node_id == second.entries[1].node_id
    assert first.entries[1].result_digest != second.entries[1].result_digest
    assert first.digest != second.digest


def test_seal_rejects_tampered_result_text(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    with SQLiteStore(db_path) as store:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "seal"})
        child = Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock-1"},
            result={"text": "ok"},
        )
        store.put(root)
        store.put(child)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE nodes SET result = ? WHERE id = ?", ('{"text":"bad"}', child.id))
    conn.commit()
    conn.close()

    with SQLiteStore(db_path) as store, pytest.raises(IntegrityError, match="result digest"):
        seal(store, root.id)
