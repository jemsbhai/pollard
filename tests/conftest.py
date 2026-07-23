import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from pollard import (
    HashRopeStore,
    KafkaStore,
    MemoryStore,
    MongoStore,
    Neo4jStore,
    PostgresStore,
    RedisStore,
    SQLiteStore,
)
from pollard.store import Store

pytest_plugins = ["pytester"]


_STORE_PARAMS = ["memory", "sqlite-interned", "sqlite-plain", "hashrope"]
if os.environ.get("POLLARD_TEST_POSTGRES_DSN"):
    _STORE_PARAMS.extend(["postgres-interned", "postgres-plain"])
if os.environ.get("POLLARD_TEST_REDIS_URL"):
    _STORE_PARAMS.append("redis")
if os.environ.get("POLLARD_TEST_MONGODB_URI"):
    _STORE_PARAMS.append("mongodb")
if os.environ.get("POLLARD_TEST_NEO4J_URI"):
    _STORE_PARAMS.append("neo4j")
if os.environ.get("POLLARD_TEST_KAFKA_BOOTSTRAP"):
    _STORE_PARAMS.append("kafka")


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
    if request.param == "redis":
        with RedisStore(
            os.environ["POLLARD_TEST_REDIS_URL"],
            store_id=f"test-{uuid4().hex}",
        ) as redis_store:
            yield redis_store
        return
    if request.param == "mongodb":
        with MongoStore(
            os.environ["POLLARD_TEST_MONGODB_URI"],
            database=os.environ.get("POLLARD_TEST_MONGODB_DATABASE", "pollard_test"),
            store_id=f"test-{uuid4().hex}",
        ) as mongo_store:
            yield mongo_store
        return
    if request.param == "neo4j":
        with Neo4jStore(
            os.environ["POLLARD_TEST_NEO4J_URI"],
            (
                os.environ.get("POLLARD_TEST_NEO4J_USER", "neo4j"),
                os.environ["POLLARD_TEST_NEO4J_PASSWORD"],
            ),
            database=os.environ.get("POLLARD_TEST_NEO4J_DATABASE", "neo4j"),
            store_id=f"test-{uuid4().hex}",
        ) as neo4j_store:
            yield neo4j_store
        return
    if request.param == "kafka":
        bootstrap = os.environ["POLLARD_TEST_KAFKA_BOOTSTRAP"]
        topic = f"pollard-test-{uuid4().hex}"
        from confluent_kafka.admin import (  # type: ignore[attr-defined]
            AdminClient,
            NewTopic,
        )

        admin = AdminClient({"bootstrap.servers": bootstrap})
        admin.create_topics(
            [
                NewTopic(
                    topic,
                    num_partitions=1,
                    replication_factor=1,
                    config={
                        "cleanup.policy": "delete",
                        "retention.ms": "-1",
                        "retention.bytes": "-1",
                    },
                )
            ]
        )[topic].result(timeout=10)
        try:
            with KafkaStore(
                {"bootstrap.servers": bootstrap}, topic=topic
            ) as kafka_store:
                yield kafka_store
        finally:
            admin.delete_topics([topic])[topic].result(timeout=10)
        return
    with SQLiteStore(
        tmp_path / f"{request.param}.db",
        intern_payloads=request.param == "sqlite-interned",
    ) as sqlite_store:
        yield sqlite_store
