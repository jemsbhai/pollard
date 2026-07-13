import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pollard._canon import IdentityValue, canonical_bytes


def test_canonical_bytes_sorts_keys_and_uses_compact_json() -> None:
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_canonical_bytes_preserves_unicode_utf8() -> None:
    assert canonical_bytes({"text": "caf\u00e9"}) == '{"text":"caf\u00e9"}'.encode()


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        {"x": 0.1},
        b"bytes",
        {"x": b"bytes"},
        {1: "not-str"},
        {"x": {"set"}},
    ],
)
def test_canonical_bytes_rejects_unsupported_identity_values(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_bytes(value)  # type: ignore[arg-type]


key_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=10,
)
leaf = st.none() | st.booleans() | st.integers() | st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=20,
)
identity_values: st.SearchStrategy[IdentityValue] = st.recursive(
    leaf,
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(key_text, children, max_size=5),
    max_leaves=20,
)


@given(identity_values)
def test_canonical_bytes_are_deterministic(value: IdentityValue) -> None:
    first = canonical_bytes(value)
    second = canonical_bytes(value)
    assert first == second
    assert json.loads(first.decode("utf-8")) == value
