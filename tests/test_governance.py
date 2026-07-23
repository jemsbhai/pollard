import json
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pollard import (
    IntegrityError,
    MemoryStore,
    SQLiteStore,
    export_subtree,
    gc,
    import_subtree,
)
from pollard.store import Store
from pollard.tree import Node, NodeKind


def _tree(store: Store) -> tuple[Node, Node, Node, Node]:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "governance"})
    keep = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"label": "keep"})
    pruned = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"label": "pruned", "body": "x" * 2048},
        meta={"pruned": True},
    )
    descendant = Node.make(
        kind=NodeKind.NOTE,
        parent=pruned.id,
        payload={"label": "descendant"},
    )
    for node in (root, keep, pruned, descendant):
        store.put(node)
    return root, keep, pruned, descendant


def test_gc_drops_only_pruned_subtrees_and_seals_survivors(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "gc.db") as store:
        root, keep, pruned, descendant = _tree(store)
        report = gc(store)
        assert report.removed_nodes == tuple(sorted((pruned.id, descendant.id)))
        assert store.exists(root.id)
        assert store.exists(keep.id)
        assert not store.exists(pruned.id)
        assert not store.exists(descendant.id)
        assert root.id in report.survivor_seals

        compacted = gc(store, mode="compact")
        assert compacted.removed_blobs == 1
        assert store.get(keep.id) == keep


def test_gc_works_for_every_builtin_store(store: Store) -> None:
    if type(store).__name__ == "KafkaStore":
        with pytest.raises(TypeError, match="does not support offline garbage"):
            gc(store)
        return
    root, keep, pruned, descendant = _tree(store)
    report = gc(store)
    assert report.removed_nodes == tuple(sorted((pruned.id, descendant.id)))
    assert store.get(root.id) == root
    assert store.get(keep.id) == keep
    assert gc(store, mode="compact").removed_nodes == ()


@given(st.lists(st.booleans(), min_size=1, max_size=30))
def test_gc_property_never_removes_unmarked_siblings(pruned_flags: list[bool]) -> None:
    store = MemoryStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "property"})
    store.put(root)
    nodes = []
    for index, marked in enumerate(pruned_flags):
        node = Node.make(
            kind=NodeKind.NOTE,
            parent=root.id,
            payload={"index": index},
            attempt=index,
            meta={"pruned": marked},
        )
        store.put(node)
        nodes.append(node)
    gc(store)
    assert store.exists(root.id)
    for node, marked in zip(nodes, pruned_flags, strict=True):
        assert store.exists(node.id) is not marked


def test_gc_rejects_tampering_before_deleting_anything(tmp_path: Path) -> None:
    path = tmp_path / "tampered.db"
    with SQLiteStore(path) as store:
        _root, _keep, pruned, _descendant = _tree(store)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE nodes SET payload = ? WHERE id = ?", ('{"bad":true}', pruned.id))
    with SQLiteStore(path, intern_payloads=False) as store:
        with pytest.raises(IntegrityError):
            gc(store)
        assert store.exists(pruned.id)


def test_export_import_round_trip_and_idempotence(tmp_path: Path) -> None:
    source = MemoryStore()
    root, keep, _pruned, _descendant = _tree(source)
    path = tmp_path / "subtree.json"
    exported = export_subtree(source, root.id, path)

    with SQLiteStore(tmp_path / "target.db") as target:
        imported = import_subtree(path, target)
        assert imported.digest == exported.digest
        assert imported.imported == 4
        assert target.get(keep.id) == keep
        again = import_subtree(path, target)
        assert again.imported == 0
        assert again.existing == 4


def test_detached_subtree_requires_parent_then_imports(tmp_path: Path) -> None:
    source = MemoryStore()
    root, keep, _pruned, _descendant = _tree(source)
    path = tmp_path / "detached.json"
    export_subtree(source, keep.id, path)
    target = MemoryStore()
    with pytest.raises(IntegrityError, match="parent is missing"):
        import_subtree(path, target)
    target.put(root)
    assert import_subtree(path, target).imported == 1


def test_tampered_manifest_is_rejected_before_target_write(tmp_path: Path) -> None:
    source = MemoryStore()
    root, _keep, _pruned, _descendant = _tree(source)
    path = tmp_path / "tampered.json"
    export_subtree(source, root.id, path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["nodes"][1]["result"] = '{"changed":true}'
    path.write_text(json.dumps(manifest), encoding="utf-8")

    target = MemoryStore()
    with pytest.raises(IntegrityError):
        import_subtree(path, target)
    assert target.roots() == []


def test_manifest_cycle_is_rejected_before_tree_walk(tmp_path: Path) -> None:
    source = MemoryStore()
    root, _keep, _pruned, _descendant = _tree(source)
    path = tmp_path / "cycle.json"
    export_subtree(source, root.id, path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["nodes"][0]["parent"] = manifest["nodes"][1]["id"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="parent"):
        import_subtree(path, MemoryStore())
