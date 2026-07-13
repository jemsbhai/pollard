import pytest

from pollard.hashing import result_digest_from_text
from pollard.tree import Node, NodeKind


def test_make_root_computes_identity() -> None:
    node = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "demo"})
    assert node.id == node.expected_id
    assert node.kind == "root"


def test_node_result_digest_is_computed_from_stored_text() -> None:
    node = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"}).id,
        payload={"model": "mock-1"},
        result={"text": "ok"},
    )
    assert node.result_text == '{"text":"ok"}'
    assert node.result_digest == result_digest_from_text(node.result_text)


@pytest.mark.parametrize(
    ("kind", "parent", "message"),
    [
        ("root", "0" * 64, "root nodes cannot have parents"),
        ("model_call", None, "non-root nodes require"),
    ],
)
def test_parent_invariants(kind: str, parent: str | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Node(id="0" * 64, parent=parent, kind=kind, attempt=0, payload={})


@pytest.mark.parametrize("attempt", [-1, True, "1"])
def test_attempt_must_be_non_negative_int(attempt: object) -> None:
    with pytest.raises(ValueError, match="attempt"):
        Node(
            id="0" * 64,
            parent=None,
            kind="root",
            attempt=attempt,  # type: ignore[arg-type]
            payload={"run": "x"},
        )


def test_from_storage_allows_identity_mismatch_for_verification() -> None:
    node = Node.from_storage(
        id="0" * 64,
        parent=None,
        kind="root",
        attempt=0,
        payload_text='{"run":"tampered"}',
        result_text=None,
        result_digest=None,
        meta_text="{}",
    )
    assert node.id != node.expected_id
