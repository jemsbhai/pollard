import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from pollard import HashRopeStore, MemoryStore, PostgresStore, SQLiteStore
from pollard.store import Store

pytest_plugins = ["pytester"]


_STORE_PARAMS = ["memory", "sqlite-interned", "sqlite-plain", "hashrope"]
if os.environ.get("POLLARD_TEST_POSTGRES_DSN"):
    _STORE_PARAMS.extend(["postgres-interned", "postgres-plain"])


@pytest.fixture(params=_STORE_PARAMS)
def store(request: pytest.FixtureRequest, tmp_path) -> Iterator[Store]:  # type: ignore[no-untyped-def]
    if request.param == "memory":
        yield MemoryStore()
        return
    if request.param == "hashrope":
        yield HashRopeStore()
        return
    if str(request.param).startswith("postgres"):
        dsn = os.environ["POLLARD_TEST_POSTGRES_DSN"]
        with PostgresStore(
            dsn,
            store_id=f"test-{uuid4().hex}",
            intern_payloads=request.param == "postgres-interned",
        ) as postgres_store:
            yield postgres_store
        return
    with SQLiteStore(
        tmp_path / f"{request.param}.db",
        intern_payloads=request.param == "sqlite-interned",
    ) as sqlite_store:
        yield sqlite_store
