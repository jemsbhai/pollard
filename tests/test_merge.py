from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pollard import IntegrityError, MemoryStore, SQLiteStore, merge, verify
from pollard._canon import canonical_bytes
from pollard.store import Store
from pollard.tree import Node, NodeKind


def _root(store: Store, label: str = "merge") -> Node:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label})
    store.put(root)
    return root


def test_merge_copies_missing_nodes_and_is_idempotent() -> None:
    source = MemoryStore()
    root = _root(source)
    child = Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"value": 1})
    source.put(child)
    destination = MemoryStore()

    first = merge(destination, source)
    second = merge(destination, source)

    assert first.to_dict() == {
        "copied": 2,
        "existing": 0,
        "result_conflicts": 0,
        "meta_conflicts": 0,
    }
    assert second.copied == 0
    assert second.existing == 2
    assert list(destination.walk(root.id)) == list(source.walk(root.id))


def test_merge_unions_meta_and_records_scalar_and_result_conflicts() -> None:
    left = MemoryStore()
    right = MemoryStore()
    left_root = _root(left)
    right_root = _root(right)
    left_node = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=left_root.id,
        payload={"model": "mock"},
        result={"text": "left"},
        meta={"worker": "left", "nested": {"a": 1}, "tags": ["left"]},
    )
    right_node = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=right_root.id,
        payload={"model": "mock"},
        result={"text": "right"},
        meta={"worker": "right", "nested": {"b": 2}, "tags": ["right"]},
    )
    left.put(left_node)
    right.put(right_node)

    report = merge(left, right)
    stored = left.get(left_node.id)

    assert report.result_conflicts == 1
    assert report.meta_conflicts == 1
    assert stored.result == {"text": "left"}
    assert stored.meta["nested"] == {"a": 1, "b": 2}
    assert stored.meta["tags"] == ["left", "right"]
    assert stored.meta["merge_conflicts"] == [
        {"path": "worker", "values": ["left", "right"]}
    ]
    assert stored.meta["result_conflicts"][0]["result"] == {"text": "right"}
    assert merge(left, right).result_conflicts == 0


def test_replay_merge_rejects_result_conflict_without_mutating() -> None:
    left = MemoryStore()
    right = MemoryStore()
    root = _root(left)
    right.put(root)
    left.put(
        Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock"},
            result={"text": "left"},
        )
    )
    right.put(
        Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock"},
            result={"text": "right"},
        )
    )
    before = list(left.walk(root.id))
    with pytest.raises(IntegrityError, match="replay"):
        merge(left, right, replay=True)
    assert list(left.walk(root.id)) == before


def test_merge_is_commutative_apart_from_kept_scalar_value() -> None:
    first = MemoryStore()
    second = MemoryStore()
    root = _root(first, "commutative")
    second.put(root)
    first_node = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"value": 1},
        meta={"worker": "a", "left": True},
    )
    second_node = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"value": 1},
        meta={"worker": "b", "right": True},
    )
    first.put(first_node)
    second.put(second_node)

    first_destination = MemoryStore()
    second_destination = MemoryStore()
    merge(first_destination, first)
    merge(first_destination, second)
    merge(second_destination, second)
    merge(second_destination, first)

    first_merged = first_destination.get(first_node.id)
    second_merged = second_destination.get(first_node.id)
    assert first_merged.identity_tuple() == second_merged.identity_tuple()
    assert first_merged.meta["left"] is True
    assert first_merged.meta["right"] is True
    assert first_merged.meta["merge_conflicts"] == second_merged.meta["merge_conflicts"]


@given(st.sets(st.integers(min_value=0, max_value=40), max_size=30))
def test_merge_property_union_is_idempotent_and_verify_clean(values: set[int]) -> None:
    left = MemoryStore()
    right = MemoryStore()
    root = _root(left, "property")
    right.put(root)
    for value in values:
        node = Node.make(
            kind=NodeKind.NOTE,
            parent=root.id,
            payload={"value": value},
        )
        (left if value % 2 else right).put(node)
    merge(left, right)
    snapshot = list(left.walk(root.id))
    merge(left, right)
    assert list(left.walk(root.id)) == snapshot
    assert verify(left, root.id).ok


def test_merge_one_thousand_nodes_preserves_rehydrated_payload_bytes(
    tmp_path: Path,
) -> None:
    with SQLiteStore(tmp_path / "source.db", intern_threshold=64) as source:
        root = _root(source, "large-merge")
        expected: dict[str, bytes] = {root.id: canonical_bytes(root.payload)}
        for index in range(1_000):
            node = Node.make(
                kind=NodeKind.NOTE,
                parent=root.id,
                payload={"index": index, "body": f"payload-{index}-" + "x" * 128},
            )
            source.put(node)
            expected[node.id] = canonical_bytes(node.payload)
        with SQLiteStore(tmp_path / "destination.db", intern_threshold=64) as destination:
            report = merge(destination, source)
            assert report.copied == 1_001
            assert verify(destination, root.id).ok
            assert {
                node.id: canonical_bytes(node.payload)
                for node in destination.walk(root.id)
            } == expected
