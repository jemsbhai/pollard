from dataclasses import replace

import pytest

from pollard import MemoryStore
from pollard.errors import IntegrityError
from pollard.tree import Node, NodeKind


def test_memory_store_rejects_parent_that_does_not_exist() -> None:
    store = MemoryStore()
    child = Node.make(kind=NodeKind.NOTE, parent="0" * 64, payload={"note": "x"})
    with pytest.raises(KeyError):
        store.put(child)


def test_memory_store_rejects_bad_identity() -> None:
    store = MemoryStore()
    node = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "r"})
    with pytest.raises(IntegrityError):
        store.put(replace(node, id="0" * 64))
