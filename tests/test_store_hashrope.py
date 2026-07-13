from dataclasses import replace

import pytest

from pollard import HashRopeStore
from pollard.errors import IntegrityError
from pollard.tree import Node, NodeKind


def test_hashrope_store_replays_snapshot() -> None:
    store = HashRopeStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "hashrope"})
    child = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1"},
        result={"text": "first"},
    )
    conflict = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1"},
        result={"text": "second"},
    )
    store.put(root)
    store.put(child)
    store.put(conflict)
    store.update_meta(child.id, {"label": "kept"})
    store.validate_log()

    replayed = HashRopeStore(store.to_bytes())

    assert replayed.get(root.id) == root
    assert replayed.get(child.id).result == {"text": "first"}
    assert replayed.get(child.id).meta["result_conflicts"][0]["result"] == {"text": "second"}
    assert replayed.get(child.id).meta["label"] == "kept"
    assert replayed.content_hash() == store.content_hash()


def test_hashrope_store_rejects_parent_that_does_not_exist() -> None:
    store = HashRopeStore()
    child = Node.make(kind=NodeKind.NOTE, parent="0" * 64, payload={"note": "x"})
    with pytest.raises(KeyError):
        store.put(child)


def test_hashrope_store_rejects_bad_identity() -> None:
    store = HashRopeStore()
    node = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
    with pytest.raises(IntegrityError):
        store.put(replace(node, id="0" * 64))
