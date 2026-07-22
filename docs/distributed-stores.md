# Distributed Store Operations

This guide covers Redis, MongoDB, Kafka, and Neo4j. PostgreSQL has a separate
[operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md).
Keep connection strings and credentials outside source control.

## Capability Matrix

| Backend | Store | Shared budgets and windows | Renewal | Physical GC |
|---|---:|---:|---:|---:|
| Redis | Yes | Yes | Yes | Offline logical-node deletion |
| MongoDB | Yes | Yes | Yes | Offline logical-node deletion |
| Neo4j | Yes | Yes | Yes | Offline logical-node deletion |
| Kafka | Yes | No | No | No |

Redis, MongoDB, and Neo4j serialize each logical store's writes. This
conservative design gives exact Decimal accounting, stable retry digests, and
server-time leases. Completed reservation records remain available so a lost
commit acknowledgement can be retried with the same reservation id. A changed
request or settlement charge is an integrity error.

Kafka is different. Its topic orders commands, but broker transactions do not
compare a proposed reservation with current arbitrary budget state. Pollard
therefore exposes Kafka only as a Store. Each Runtime checks its own budget;
several runtimes do not share a Kafka limit.

## Redis

Install and connect:

```powershell
pip install "pollard[redis]"
$env:POLLARD_REDIS_URL = "rediss://user:password@redis.example:6379/0"
```

```python
import os
from pollard import RedisStore

store = RedisStore(os.environ["POLLARD_REDIS_URL"], store_id="support-prod")
```

Pollard uses `WATCH`, `MULTI`, and `EXEC` on one revision key. Every key for a
logical store uses one SHA-256-derived Redis Cluster hash tag. Exact arithmetic
runs with Python `Decimal`; it is not narrowed through Redis numeric commands.

Production requirements:

- Set `maxmemory-policy noeviction`. Eviction can remove coordination state.
- Enable persistent storage. Use an AOF policy and backup schedule that match
  the accepted recovery point.
- Use TLS and ACLs, and restrict commands that can delete or rewrite Pollard
  hashes.
- Treat primary failover as a durability boundary. Redis asynchronous
  replication can lose an acknowledged write; `WAIT` does not make the system
  strongly consistent.
- Keep every worker on the same endpoint, prefix, and `store_id`.

`reconnect()` replaces the client and refuses a missing identity, missing
revision, or unknown schema. If a reservation or settlement remains uncertain
after one reconnect and idempotent retry, Pollard raises the existing typed
uncertainty exception.

## MongoDB

Install and connect:

```powershell
pip install "pollard[mongodb]"
$env:POLLARD_MONGODB_URI = "mongodb://user:password@db.example/pollard?replicaSet=rs0"
```

```python
import os
from pollard import MongoStore

store = MongoStore(
    os.environ["POLLARD_MONGODB_URI"],
    database="pollard",
    store_id="support-prod",
)
```

MongoStore requires a replica set or sharded deployment and refuses a
standalone server. Transactions use snapshot reads, majority writes, and a
per-store coordinator document. Pollard stores Decimal values as strings so
MongoDB Decimal128 limits do not narrow accounting values.

Use at least three data-bearing members for production fault tolerance. Apply
TLS, authentication, least-privilege collection access, point-in-time backups,
and tested replica-set recovery. A single-member replica set is suitable only
for local transaction testing. Driver transaction callbacks can run more than
once, so application code must not be placed inside a store transaction.

## Neo4j

Install and connect:

```powershell
pip install "pollard[neo4j]"
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
```

```python
import os
from pollard import Neo4jStore

store = Neo4jStore(
    os.environ["POLLARD_NEO4J_URI"],
    ("pollard_app", os.environ["POLLARD_NEO4J_PASSWORD"]),
    database="neo4j",
    store_id="support-prod",
)
```

Each logical store has one coordinator node. A write increments its revision
before reading mutable state, which obtains an explicit write lock and prevents
read-modify-write loss under Neo4j's default isolation. Server realtime is
sampled after that lock. Sessions use write routing even for structural reads
so another process does not read a stale follower immediately after a commit.

The application role needs permission to create the two uniqueness constraints
on first use and to read, create, update, and delete Pollard-labeled nodes. An
administrator can create the constraints first and then reduce application
privileges. Configure encrypted Bolt, authentication, backups, and primary
failover according to the deployment's Neo4j edition and topology.

## Kafka

Install and connect only after provisioning the topic:

```powershell
pip install "pollard[kafka]"
```

```python
from pollard import KafkaStore

store = KafkaStore(
    {"bootstrap.servers": "broker-1:9092,broker-2:9092"},
    topic="pollard-support-prod",
)
```

One dedicated topic is one logical Pollard store. Pollard validates all of the
following before replay:

- exactly partition `0` exists;
- `cleanup.policy=delete` without compaction;
- `retention.ms=-1` and `retention.bytes=-1`;
- the log still begins at offset zero; and
- every canonical event has the expected version, store key, offset, and
  application operation digest.

The producer forces `acks=all` and idempotence. A direct read-committed consumer
rebuilds an in-memory view and consumes through each acknowledged command
offset before the Store method returns. Cold start is linear in log size, and
the view uses memory proportional to stored nodes and operation outcomes.
Broker message-size limits apply to canonical node events.

Do not enable compaction or finite retention later. Do not add partitions.
KafkaStore has no physical GC method because removing an old command would
break complete replay. Use topic-level retention and deletion under the
operator's data policy, or choose a record-deletable backend. A topic in the
same cluster is not independent external seal custody.

## Recovery And External Custody

For every backend, stop provider traffic before a destructive restore. Restore
into a separate target, connect with a new store instance, run `verify()` for
required roots, compare `seal()` digests with independently retained custody
records, inspect shared budget state, and run strict replay with callables that
fail if executed.

`SQLiteSealSink` remains a reference independent custody target. Store its file
under different credentials and backup control from the primary database or
broker. No backend can make a seal independent by writing it beside the data it
is meant to check.

## Upstream References

- [Redis transactions and optimistic locking](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/)
  and [Redis `WAIT` durability limits](https://redis.io/docs/latest/commands/wait/)
- [MongoDB Python transactions](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/)
  and [production transaction considerations](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)
- [Neo4j Python managed transactions](https://neo4j.com/docs/python-manual/current/transactions/)
  and [concurrent data access](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/)
- [Apache Kafka design](https://kafka.apache.org/documentation/)
  and [Confluent Python client API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html)
