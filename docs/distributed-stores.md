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

## Choose A Backend

PostgreSQL remains the default choice when an application needs mature
transactional durability, shared limits, and familiar backup and failover
operations. Select another backend when its existing operational platform is a
material advantage and its limits below are acceptable.

| Backend | Good fit | Do not select it when |
|---|---|---|
| Redis | Low-latency coordination on an already durable, persistent Redis deployment | An acknowledged write must survive asynchronous failover with PostgreSQL-like guarantees |
| MongoDB | The application already operates replica-set or sharded transactions | Only a standalone server is available or transaction retry behavior is not understood |
| Neo4j | Pollard records should live beside graph-managed application data | One coordinator node per logical store is an unacceptable write bottleneck |
| Kafka | Complete ordered audit and replay is required without shared limits | Shared budgets, physical record GC, bounded cold-start time, or finite retention is required |

The URL path creates a standard redis-py `Redis` client. A caller-owned
`client_factory` can return a fresh Sentinel-managed primary client during
construction and reconnect. Redis Cluster remains outside the supported
release matrix because Pollard's complete server-time transaction path has not
passed cluster acceptance.

Detailed deployment and recovery guides:

- [Redis operations](https://github.com/jemsbhai/pollard/blob/main/docs/redis-operations.md)
- [MongoDB operations](https://github.com/jemsbhai/pollard/blob/main/docs/mongodb-operations.md)
- [Neo4j operations](https://github.com/jemsbhai/pollard/blob/main/docs/neo4j-operations.md)
- [Kafka operations](https://github.com/jemsbhai/pollard/blob/main/docs/kafka-operations.md)

## End-To-End Configured Example

The repository's `examples/09_distributed_stores.py` program opens one selected
backend, records a deterministic model-shaped call without contacting a model
provider, replays it with a callable that fails if executed, verifies the tree,
and creates a seal. It does not print a connection string or credential.

```powershell
python -m pip install -e ".[stores]"
python examples\09_distributed_stores.py --help
```

Configure one backend, then run it. A MongoDB example is:

```powershell
$env:POLLARD_MONGODB_URI = "mongodb://user:password@db.example/pollard?replicaSet=rs0"
$env:POLLARD_MONGODB_DATABASE = "pollard"
python examples\09_distributed_stores.py `
  --backend mongodb `
  --store-id support-prod
```

| Selector | Required environment | Optional environment |
|---|---|---|
| `postgresql` | `POLLARD_PG_DSN` | None |
| `redis` | `POLLARD_REDIS_URL` | `POLLARD_REDIS_PREFIX` |
| `mongodb` | `POLLARD_MONGODB_URI` | `POLLARD_MONGODB_DATABASE` |
| `neo4j` | `POLLARD_NEO4J_URI`, `POLLARD_NEO4J_PASSWORD` | `POLLARD_NEO4J_USER`, `POLLARD_NEO4J_DATABASE` |
| `kafka` | `POLLARD_KAFKA_BOOTSTRAP`, `POLLARD_KAFKA_TOPIC` | None |

Expected JSON includes `verified: true`, `replay_matched: true`, and a seal
digest. `shared_arbiter` is false only for Kafka. The program contacts the
configured database or broker, leaves its run in that store, makes no hosted
model request, and incurs zero model-provider spend.

The example settles a one-step budget. Do not reuse its run label for another
record attempt: fail-closed accounting will refuse it before the local callable
executes. Pass a fresh `--run-label` for each new recording. The command already
performs strict replay of the recording it just created.

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

Applications that use Sentinel or a managed-primary resolver should pass a
fresh synchronous redis-py client through `client_factory`. The client must
decode responses as strings. Pollard calls the factory again on reconnect and
does not own the resolver, Sentinel credentials, or failover policy. See the
[Redis operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/redis-operations.md)
for a complete example.

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

Create the dedicated topic explicitly. Set the replication factor to the
cluster's production durability policy. Run this from a Kafka administration
environment where `kafka-topics.sh` is on `PATH`:

```powershell
kafka-topics.sh `
  --bootstrap-server broker-1:9092 `
  --create `
  --topic pollard-support-prod `
  --partitions 1 `
  --replication-factor 3 `
  --config cleanup.policy=delete `
  --config retention.ms=-1 `
  --config retention.bytes=-1
```

TLS and SASL settings remain caller-owned confluent-kafka configuration. Pass
them in `client_config` rather than committing them or placing them in node
payloads. Pollard overrides acknowledgement, idempotence, consumer offset, and
isolation settings required by its replay contract; it passes unrelated
transport and authentication options through to the clients.

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

## Lifecycle, Reconnect, And Uncertain Outcomes

All five remote stores are synchronous context managers. Keep a store open for
the application's worker lifetime and close it during orderly shutdown:

```python
import os

from pollard import RedisStore, Runtime

with RedisStore(
    os.environ["POLLARD_REDIS_URL"],
    store_id="support-prod",
) as store:
    runtime = Runtime(store)
    # Run application work here.
```

`close()` is safe during normal cleanup. `reconnect()` replaces the underlying
connection or client pool and refuses a missing or incompatible Pollard schema
before returning. Do not call `reconnect()` concurrently with work on the same
store object; pause that worker or replace the object under the application's
own synchronization.

Redis, MongoDB, Neo4j, and PostgreSQL can lose a connection after the server
commits but before the client receives acknowledgement. Pollard retries an
exact reservation or settlement once with the same identity. If the outcome
still cannot be confirmed, it raises `ReservationUncertain` or
`SettlementUncertain` instead of treating the operation as absent. A lease
that cannot be renewed through its deadline becomes `ReservationLeaseLost`.

Use this incident sequence:

1. Stop new provider and tool dispatch for the affected logical store.
2. Retain the content-free exception type, reservation id, store id, and time.
3. Reconnect and verify the schema and required run roots.
4. Reconcile the reservation using the same id, request, and settlement
   charges. Never invent a replacement id for an ambiguous operation.
5. Until reconciliation proves otherwise, account for an ambiguous dispatched
   call at its conservative reserved ceiling.
6. Resume traffic only after active leases and independently retained seals
   agree with the intended continuation point.

Kafka has no reservation methods and therefore does not raise the arbiter
uncertainty types. It uses deterministic operation ids, broker acknowledgement,
and replay confirmation. A Kafka integrity or uncertain-write error should
stop writes to that topic until `reconnect()`, complete replay, and required
root verification succeed.

## Logical Isolation And Authorization

`store_id` separates records for PostgreSQL, Redis, MongoDB, and Neo4j. It is
not an authorization or encryption boundary. Use database roles, ACLs, TLS,
network policy, and separate databases or accounts when tenants require access
isolation. Every worker sharing a limit must use the same physical backend,
logical store id, meter configuration, and run root.

Kafka requires a dedicated topic for each logical store. Do not place two
store ids or unrelated producers in one topic; replay refuses an unexpected
message key or event. Topic ACLs are the authorization boundary.

First construction initializes the current schema for an empty logical store.
Later construction refuses an unknown version. Only PostgreSQL exposes a
legacy schema migration method; the other adapters do not guess how to rewrite
future or externally modified data.

## Move Existing Recordings

`merge(destination, source)` copies node trees but does not copy active budget
reservations, settled budget counters, rate-window events, or leases. Drain
calls before moving a recording and start a new governance scope in the target.
For example, copy an offline SQLite recording into Redis as follows:

```python
import os

from pollard import RedisStore, SQLiteStore, merge, seal, verify

with (
    RedisStore(os.environ["POLLARD_REDIS_URL"], store_id="import-2026-07") as target,
    SQLiteStore("recording.db") as source,
):
    report = merge(target, source, replay=True)
    for root_id in source.roots():
        assert verify(target, root_id).ok
        print(root_id, seal(target, root_id).digest)
    print(report.to_dict())
```

Import into an empty logical store and compare the printed digests with seals
kept outside both stores. Kafka import appends the complete node history and is
not physically reversible through Pollard. The CLI does not construct Redis,
MongoDB, Neo4j, or Kafka clients; perform these moves through the Python API so
the application owns credentials and transport policy.

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

Before production enablement, run a deployment-specific acceptance check that
uses non-production data and covers concurrent exact reservations, process
restart, primary failover, credential rotation, backup restore into a separate
target, strict replay, `verify()`, and comparison with an externally retained
seal. The release's one-node containers prove adapter behavior and persistence,
not safety during a multi-node network partition.

## Upstream References

- [Redis transactions and optimistic locking](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/)
  and [Redis `WAIT` durability limits](https://redis.io/docs/latest/commands/wait/)
- [MongoDB Python transactions](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/)
  and [production transaction considerations](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)
- [Neo4j Python managed transactions](https://neo4j.com/docs/python-manual/current/transactions/)
  and [concurrent data access](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/)
- [Apache Kafka design](https://kafka.apache.org/documentation/)
  and [Confluent Python client API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html)
