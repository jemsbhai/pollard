import hashlib
import importlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pollard import IntegrityError, MemoryStore, SQLiteStore, merge, verify
from pollard._canon import canonical_bytes
from pollard.hashing import result_digest_from_text
from pollard.merge import _merge_prepared, _MergeSpool
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


def test_merge_spool_preserves_deterministic_order_and_exact_node_text(
    tmp_path: Path,
) -> None:
    source = MemoryStore()
    root = _root(source, "spool-fidelity")
    result_text = '{ "text" : "caf\\u00e9", "values" : [1, 2] }'
    child = Node.from_storage(
        id=Node.make(
            kind=NodeKind.MODEL_CALL,
            parent=root.id,
            payload={"model": "mock", "unicode": "é"},
        ).id,
        parent=root.id,
        kind=NodeKind.MODEL_CALL.value,
        attempt=0,
        payload_text='{"model":"mock","unicode":"é"}',
        result_text=result_text,
        result_digest=result_digest_from_text(result_text),
        meta_text='{"nested":{"b":2,"a":1},"tags":["\\u03b2","\\u03b1"]}',
    )
    source.put(child)

    spool = _MergeSpool.prepare(tmp_path / "prepared.sqlite3", source)
    first = list(spool.iter_nodes())
    second = list(spool.iter_nodes())

    assert [node.id for node in first] == [root.id, child.id]
    assert first == second
    assert first[1].result_text == result_text
    assert first[1].result_digest == child.result_digest
    assert canonical_bytes(first[1].payload) == canonical_bytes(child.payload)
    assert first[1].meta == child.meta
    assert list(first[1].meta) == list(child.meta)
    assert list(first[1].meta["nested"]) == list(child.meta["nested"])
    assert vars(spool) == {
        "path": tmp_path / "prepared.sqlite3",
        "count": 2,
        "digest": spool.digest,
    }

    first_destination = MemoryStore()
    second_destination = MemoryStore()
    assert _merge_prepared(first_destination, spool).to_dict() == _merge_prepared(
        second_destination,
        spool,
    ).to_dict()
    assert list(first_destination.walk(root.id)) == list(
        second_destination.walk(root.id)
    )


def test_merge_spool_is_disk_backed_for_multiple_large_sources(
    tmp_path: Path,
) -> None:
    spools: list[_MergeSpool] = []
    for source_index in range(3):
        source = MemoryStore()
        root = _root(source, f"large-spool-{source_index}")
        for node_index in range(48):
            source.put(
                Node.make(
                    kind=NodeKind.NOTE,
                    parent=root.id,
                    payload={
                        "index": node_index,
                        "body": f"{source_index}:{node_index}:" + "x" * 16_384,
                    },
                )
            )
        spools.append(
            _MergeSpool.prepare(
                tmp_path / f"prepared-{source_index}.sqlite3",
                source,
            )
        )

    assert len({spool.path for spool in spools}) == 3
    assert all(spool.path.stat().st_size > 500_000 for spool in spools)
    assert all(set(vars(spool)) == {"path", "count", "digest"} for spool in spools)
    assert [len(list(spool.iter_nodes())) for spool in spools] == [49, 49, 49]


@pytest.mark.parametrize("damage", ["truncate", "append", "record", "count", "state"])
def test_merge_spool_refuses_corrupt_or_truncated_state(
    tmp_path: Path,
    damage: str,
) -> None:
    source = MemoryStore()
    root = _root(source, "corrupt-spool")
    source.put(Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"value": 1}))
    path = tmp_path / f"{damage}.sqlite3"
    spool = _MergeSpool.prepare(path, source)

    if damage == "truncate":
        with path.open("r+b") as file:
            file.truncate(max(1, path.stat().st_size // 2))
    elif damage == "append":
        with path.open("ab") as file:
            file.write(b"unexpected trailing bytes")
    else:
        connection = sqlite3.connect(path)
        try:
            if damage == "record":
                connection.execute(
                    "UPDATE nodes SET record = ? WHERE position = 1",
                    (b"{}",),
                )
            elif damage == "count":
                connection.execute(
                    "UPDATE metadata SET value = '999' WHERE key = 'count'"
                )
            else:
                connection.execute(
                    "UPDATE metadata SET value = 'writing' WHERE key = 'state'"
                )
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(IntegrityError, match=r"spool|integrity|corrupt|finalized"):
        spool.validate()
    with pytest.raises(IntegrityError, match=r"spool|integrity|corrupt|finalized"):
        list(spool.iter_nodes())


def test_merge_spool_validates_node_integrity_and_parent_order_before_replay(
    tmp_path: Path,
) -> None:
    valid = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "invalid"})
    invalid = Node(
        id="0" * 64,
        parent=None,
        kind=valid.kind,
        attempt=valid.attempt,
        payload=valid.payload,
    )

    class InvalidStore:
        def roots(self) -> list[str]:
            return [invalid.id]

        def walk(self, _root_id: str) -> Iterator[Node]:
            yield invalid

    with pytest.raises(IntegrityError, match="identity"):
        _MergeSpool.prepare(tmp_path / "invalid.sqlite3", InvalidStore())  # type: ignore[arg-type]

    parent = Node.make(
        kind=NodeKind.NOTE,
        parent=valid.id,
        payload={"value": "parent"},
    )
    child = Node.make(
        kind=NodeKind.NOTE,
        parent=parent.id,
        payload={"value": "child-before-parent"},
    )

    class ChildFirstStore:
        def roots(self) -> list[str]:
            return [valid.id]

        def walk(self, _root_id: str) -> Iterator[Node]:
            yield valid
            yield child
            yield parent

    with pytest.raises(IntegrityError, match="before its parent"):
        _MergeSpool.prepare(  # type: ignore[arg-type]
            tmp_path / "child-first.sqlite3",
            ChildFirstStore(),
        )


def test_merge_spool_manifest_rejects_a_self_consistent_record_rewrite(
    tmp_path: Path,
) -> None:
    source = MemoryStore()
    root = _root(source, "anchored-spool")
    source.put(Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"value": 1}))
    path = tmp_path / "rewritten.sqlite3"
    spool = _MergeSpool.prepare(path, source)

    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            "SELECT record FROM nodes WHERE position = 1"
        ).fetchone()
        assert stored is not None
        document = json.loads(bytes(stored[0]))
        document["meta"] = '{"tampered":true}'
        rewritten = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        connection.execute(
            "UPDATE nodes SET record = ?, digest = ? WHERE position = 1",
            (rewritten, hashlib.sha256(rewritten).hexdigest()),
        )
        rolling = hashlib.sha256()
        for (record,) in connection.execute(
            "SELECT record FROM nodes ORDER BY position"
        ):
            value = bytes(record)
            rolling.update(len(value).to_bytes(8, "big"))
            rolling.update(value)
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'digest'",
            (rolling.hexdigest(),),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(IntegrityError, match="manifest"):
        spool.validate()


def test_merge_spool_rejects_forged_result_object_and_text_pair(
    tmp_path: Path,
) -> None:
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "forged"})
    expected = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"actual": 1},
    )
    stored_text = '{"stored":2}'
    forged = Node(
        id=expected.id,
        parent=expected.parent,
        kind=expected.kind,
        attempt=expected.attempt,
        payload=expected.payload,
        result={"actual": 1},
        result_digest=result_digest_from_text(stored_text),
        meta=expected.meta,
        _result_text=stored_text,
    )

    class ForgedStore:
        def roots(self) -> list[str]:
            return [root.id]

        def walk(self, _root_id: str) -> Iterator[Node]:
            yield root
            yield forged

    with pytest.raises(IntegrityError, match="without changing"):
        _MergeSpool.prepare(  # type: ignore[arg-type]
            tmp_path / "forged.sqlite3",
            ForgedStore(),
        )


@pytest.mark.parametrize("empty", [False, True])
def test_merge_spool_requires_each_declared_root_to_start_its_walk(
    tmp_path: Path,
    empty: bool,
) -> None:
    declared = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "declared"})
    substituted = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "substituted"},
    )

    class WrongRootStore:
        def roots(self) -> list[str]:
            return [declared.id]

        def walk(self, _root_id: str) -> Iterator[Node]:
            if not empty:
                yield substituted

    with pytest.raises(IntegrityError, match="declared root"):
        _MergeSpool.prepare(  # type: ignore[arg-type]
            tmp_path / f"wrong-root-{empty}.sqlite3",
            WrongRootStore(),
        )


def test_merge_spool_closes_connection_when_read_only_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = MemoryStore()
    _root(source, "setup-failure")
    path = tmp_path / "setup-failure.sqlite3"
    spool = _MergeSpool.prepare(path, source)
    real_connect = sqlite3.connect
    closed = False

    class FailingConnection:
        def __init__(self) -> None:
            self.connection = real_connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
            )

        def execute(self, statement: str) -> object:
            if "temp_store" in statement:
                raise sqlite3.OperationalError("injected PRAGMA failure")
            return self.connection.execute(statement)

        def close(self) -> None:
            nonlocal closed
            closed = True
            self.connection.close()

    def failing_connect(*_args: object, **_kwargs: object) -> FailingConnection:
        return FailingConnection()

    merge_module = importlib.import_module("pollard.merge")
    monkeypatch.setattr(merge_module.sqlite3, "connect", failing_connect)

    with pytest.raises(IntegrityError, match="cannot be opened"):
        spool.validate()
    assert closed is True
    renamed = path.with_name("setup-failure-renamed.sqlite3")
    path.rename(renamed)
    assert renamed.is_file()
