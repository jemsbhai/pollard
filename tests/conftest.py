from collections.abc import Iterator

import pytest

from pollard import HashRopeStore, MemoryStore, SQLiteStore
from pollard.store import Store

pytest_plugins = ["pytester"]


@pytest.fixture(params=["memory", "sqlite", "hashrope"])
def store(request: pytest.FixtureRequest, tmp_path) -> Iterator[Store]:  # type: ignore[no-untyped-def]
    if request.param == "memory":
        yield MemoryStore()
        return
    if request.param == "hashrope":
        yield HashRopeStore()
        return
    with SQLiteStore(tmp_path / "store.db") as sqlite_store:
        yield sqlite_store
