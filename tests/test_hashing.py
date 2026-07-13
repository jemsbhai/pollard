from pollard.hashing import node_id, result_digest_from_text, result_text_and_digest

# Frozen vectors: changing these constants is a MAJOR version event.
ROOT_VECTOR = "56022c0c5fabdad4de7f9226bd28d33cc5c0098d7e1569b1a3b6293d833d8e8e"
MODEL_VECTOR = "07f65f2688e6b6ee1e8acab69b36e29be8d2c72c3317aa47831121a01a7c724d"
RETRY_VECTOR = "e043a7e862a99b31e253c243b0462f259e3d519fa8d6b5b545051f228fba8858"


def test_node_id_golden_vectors() -> None:
    root = node_id("root", None, 0, {"run": "golden"})
    model = node_id(
        "model_call",
        root,
        0,
        {"messages": [{"role": "user", "content": "hello"}], "model": "mock-1"},
    )
    retry = node_id(
        "model_call",
        root,
        1,
        {"messages": [{"role": "user", "content": "hello"}], "model": "mock-1"},
    )
    assert root == ROOT_VECTOR
    assert model == MODEL_VECTOR
    assert retry == RETRY_VECTOR


def test_attempt_is_the_identity_salt() -> None:
    payload = {"model": "mock-1", "messages": []}
    assert node_id("model_call", ROOT_VECTOR, 0, payload) != node_id(
        "model_call",
        ROOT_VECTOR,
        1,
        payload,
    )


def test_ancestry_changes_child_identity() -> None:
    parent_a = node_id("root", None, 0, {"run": "a"})
    parent_b = node_id("root", None, 0, {"run": "b"})
    payload = {"model": "mock-1", "messages": []}
    assert node_id("model_call", parent_a, 0, payload) != node_id(
        "model_call",
        parent_b,
        0,
        payload,
    )


def test_result_digest_uses_stored_text() -> None:
    text, digest = result_text_and_digest({"b": 2.0, "a": 1})
    assert text == '{"a":1,"b":2.0}'
    assert digest == result_digest_from_text(text)
