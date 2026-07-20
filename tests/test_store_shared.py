from dataclasses import replace

import pytest

from pollard.errors import IntegrityError
from pollard.store import Store
from pollard.tree import Node, NodeKind


def root_and_children(store: Store) -> tuple[Node, list[Node]]:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "shared"})
    store.put(root)
    children = [
        Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"label": "b"}),
        Node.make(kind=NodeKind.MODEL_CALL, parent=root.id, payload={"model": "m"}),
        Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"label": "a"}),
    ]
    for child in children:
        store.put(child)
    return root, children


def test_put_get_and_idempotent_put(store: Store) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
    store.put(root)
    store.put(root)
    assert store.get(root.id) == root


def test_children_are_sorted_by_kind_then_id(store: Store) -> None:
    root, children = root_and_children(store)
    expected = sorted(
        [child.id for child in children],
        key=lambda item: (store.get(item).kind, item),
    )
    assert store.children(root.id) == expected


def test_update_meta_merges_top_level_keys(store: Store) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"}, meta={"a": 1})
    store.put(root)
    store.update_meta(root.id, {"b": 2})
    assert store.get(root.id).meta == {"a": 1, "b": 2}


def test_walk_is_depth_first_and_deterministic(store: Store) -> None:
    root, _children = root_and_children(store)
    walked = [node.id for node in store.walk(root.id)]
    assert walked == [root.id, *store.children(root.id)]


def test_walk_handles_depth_beyond_python_recursion_limit(store: Store) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "deep-walk"})
    store.put(root)
    parent = root
    expected = [root.id]
    for index in range(1_100):
        child = Node.make(
            kind=NodeKind.NOTE,
            parent=parent.id,
            payload={"index": index},
        )
        store.put(child)
        expected.append(child.id)
        parent = child

    assert [node.id for node in store.walk(root.id)] == expected


def test_roots_are_sorted_by_run_label_then_id(store: Store) -> None:
    second = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "z-last"})
    first = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "a-first"})
    store.put(second)
    store.put(first)
    assert store.roots() == [first.id, second.id]


def test_result_conflict_keeps_first_result_and_records_conflict(store: Store) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
    store.put(root)
    first = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1"},
        result={"text": "first"},
    )
    second = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1"},
        result={"text": "second"},
    )
    store.put(first)
    store.put(second)
    stored = store.get(first.id)
    assert stored.result == {"text": "first"}
    assert stored.meta["result_conflicts"][0]["result"] == {"text": "second"}


def test_same_id_with_different_identity_fields_is_integrity_error(store: Store) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
    store.put(root)
    bad = replace(root, payload={"run": "other"})
    with pytest.raises(IntegrityError):
        store.put(bad)
