# Scale-Out Stores And Governance

Pollard's shared-arbiter path coordinates worker teams through PostgreSQL,
Redis, MongoDB, or Neo4j across multiple hosts. SQLite uses the same transaction
contract for processes sharing one database file on one host. MemoryStore,
HashRopeStore, and KafkaStore use per-runtime budget checks.

This design has one hard boundary: all workers governed by one shared limit
must use the same transactional store and logical store id. Pollard is not a
consensus system and does not coordinate disconnected databases.

| Backend | Shared arbiter | Required deployment boundary |
|---|---:|---|
| SQLite | Yes | One database file on one host |
| PostgreSQL | Yes | One database and `store_id` |
| Redis | Yes | One persistent no-eviction logical store and `store_id` |
| MongoDB | Yes | One replica set or sharded deployment and `store_id` |
| Neo4j | Yes | One primary-routed database and `store_id` |
| Kafka | No | One single-partition infinite-retention topic for Store ordering only |

Operational requirements for the added backends are in
[Distributed store operations](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md).
The
[configured walkthrough](https://github.com/jemsbhai/pollard/blob/main/examples/README.md#configured-distributed-store-walkthrough)
records, strictly replays, verifies, and seals one deterministic call against
each remote backend without contacting a model provider.

## Install And Connect

Install psycopg 3 through the optional extra:

```powershell
pip install "pollard[pg]"
```

Keep the connection string outside source control:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
```

```python
import os

from pollard import PostgresStore

store = PostgresStore(
    os.environ["POLLARD_PG_DSN"],
    store_id="support-prod",
)
```

The DSN supplies the database hostname, port, database, user, password, and any
TLS parameters required by the operator. Pollard does not read model-provider
credentials. A database role needs these privileges:

- Connect to the selected database and use the target schema.
- Create tables, indexes, and sequences on first use.
- Select, insert, update, and delete rows in Pollard's tables.
- Use the Pollard event sequence.

After an administrator creates the schema objects, an application role can be
limited to row access and sequence use. PostgreSQL network controls, TLS,
credential rotation, backups, and row-level tenant policy remain operator
responsibilities.

`store_id` separates logical Pollard stores inside one database. Workers share
governance only when both the DSN target and `store_id` match. Store ids are not
an authorization boundary; database permissions provide that boundary.

## Concurrent Node Writes

Node identity remains content-addressed. Two workers inserting the same node
race through `INSERT ... ON CONFLICT DO NOTHING`; the stored identity is the
same either way. Different identity fields under one id remain an integrity
error. Metadata updates take a row lock before merging their top-level patch.

PostgreSQL and SQLite both intern large string leaves while preserving the
canonical rehydrated payload. Interned blobs remain plaintext and follow the
same data classification as the original payload.

## Shared Budget Reservations

On a transactional store, each governed model or tool call uses three steps:

1. Precheck atomically reserves each known estimate against every active budget
   scope.
2. The caller function executes only after that transaction succeeds.
3. Settlement removes the reservation and adds the actual charge.

Reservations have a lease. A process that exits after precheck cannot hold
capacity forever; a later precheck ignores expired reservations. Pollard renews
the lease while a model or tool callable is still running:

```python
runtime = Runtime(store, reservation_lease_seconds=180)
```

If renewal cannot be confirmed before expiry, Pollard reports a lost lease
after attempting settlement and recording the completed call. Schema migration,
backup, restore, reconnect, and incident procedures are in
[PostgreSQL operations](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md).

With concurrent writers sharing one arbiter, total settled spend is bounded by
the budget plus the sum of actual-minus-estimate overshoot for calls that passed
precheck. Meters with exact prechecks, including steps and request windows, do
not exceed the limit. An approximate token estimate can still settle above its
reservation because the provider spend has already occurred.

The arbiter tables are mutable coordination state. They are not part of node
identity or subtree seals. A backup used for live continuation should include
both the node and arbiter tables.

## Sliding Windows

`WindowMeter(name, limit, window_seconds)` stores settled events and active
reservations in the selected store. The default `requests` behavior counts one
model or tool call and is exact:

```python
from pollard import Runtime, WindowMeter
from pollard.meters import StepMeter

runtime = Runtime(
    store,
    meters=[StepMeter(), WindowMeter("requests", 120, 60)],
)
```

For tokens, wrap an estimating `TokenMeter` so precheck can reserve input and
output capacity:

```python
from pollard import Runtime, WindowMeter
from pollard.estimators.openai import OpenAITokenEstimator
from pollard.meters import StepMeter, TokenMeter

token_meter = TokenMeter(
    OpenAITokenEstimator(),
    reserved_output_tokens=2_048,
)
runtime = Runtime(
    store,
    meters=[
        StepMeter(),
        WindowMeter("tokens", 100_000, 60, meter=token_meter),
    ],
)
```

Window scope is the run root plus the meter configuration. Resuming the same
run preserves the window, and workers on the same root see each other's events.
A window refusal uses `reason="window"` and records `window_seconds` in its
identity payload.

## Merge Disconnected Stores

`merge(destination, source)` copies missing nodes, validates equal-id identity,
keeps the destination result on result collisions, and records the incoming
result under mutable metadata. `merge(..., replay=True)` raises an integrity
error instead of accepting a result collision.

Metadata union never removes a key. Nested objects are combined, lists are
unioned by canonical JSON value, and conflicting scalar values are recorded
under `meta["merge_conflicts"]`. Repeating a merge does not add duplicate
conflict records. An exact rerun is idempotent.

Merge is not one cross-node or cross-backend transaction. The Python
`merge()` function preflights its selected source and known identity or replay
conflicts before copying, but a destination failure can retain nodes or metadata
already accepted. Inspect
the destination after an error and rerun the exact merge to complete an
idempotent copy. Quiesce source and destination writers for evidence-grade
transfer; concurrent destination writers can race mutable metadata updates.
Verify the destination and seal required roots after the copy.

```python
from pollard import SQLiteStore, merge

with SQLiteStore("combined.db") as destination:
    with SQLiteStore("worker.db") as source:
        report = merge(destination, source)
print(report.to_dict())
```

Use replay mode when importing a recording into a deterministic replay corpus.
Use the default keep-first behavior when joining audit ledgers where observing
the conflict is more useful than rejecting the union.

## Multi-Store CLI

`runs` accepts one or more store specifications, and `merge` accepts one
destination followed by one or more sources. `show`, `report`, `verify`, `seal`,
and `export` each accept one SQLite path, PostgreSQL store specification, or
URL-backed Redis or environment-backed MongoDB, Neo4j, or Kafka read
specification. Redis, MongoDB, Neo4j, and Kafka are accepted as merge sources;
`redis-env:`, `mongo-env:`, `neo4j-env:`, and narrowly qualified `kafka-env:`
stores are also accepted as merge destinations:

```powershell
pollard runs worker-a.db worker-b.db --json
pollard merge combined.db worker-a.db worker-b.db --json
```

A PostgreSQL URI may use its fragment as the logical store id, but placing a
password on the command line can expose it to process inspection. The preferred
form reads the DSN from an environment variable:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
pollard runs "pg-env:POLLARD_PG_DSN#support-prod" --json
pollard show "pg-env:POLLARD_PG_DSN#support-prod" <root-id>
pollard report "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --json
pollard verify "pg-env:POLLARD_PG_DSN#support-prod" --json
pollard seal "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --output seal.json --json
pollard export "pg-env:POLLARD_PG_DSN#support-prod" <root-id> subtree.json --json
pollard merge combined.db "pg-env:POLLARD_PG_DSN#support-prod" --json
```

CLI output labels the environment variable and store id, never the DSN value.
For evidence-grade PostgreSQL verification, sealing, or export, quiesce writes
or otherwise keep the store stable for the full traversal. The CLI traversal
is not a repeatable-read snapshot.

Plain SQLite paths remain compatible with earlier releases. `import` and `gc`
remain SQLite-only operations. An ordinary URL-backed Redis namespace can be
selected with `redis-env:VARIABLE?prefix=PREFIX#store-id` for observation or as
a merge source, and the same environment-backed grammar is required for a
Redis merge destination. Every destination must include an explicit prefix and
store id. Sources and destinations open with `create=False` by default and
require an existing namespace, so a missing or mistyped selector fails closed.

`--initialize-if-missing` is valid only for an explicit `redis-env:`,
`mongo-env:`, or `neo4j-env:` destination. The CLI may validate a destination
selector's syntax and flag eligibility first, but does not read the referenced
environment or construct its client. With the opt-in, after every source has
been opened, fully traversed, validated, closed, and finalized into an
independently validated private disk spool, the destination opens with
`create=True`. Redis initializes identity, schema, and revision in one Redis
transaction. MongoDB creates or validates its physical unique index before
atomically initializing schema and coordinator revision. Neo4j creates or
validates its shared global constraints separately before atomically
initializing schema and coordinator. Review every explicit selector before
using the flag because a typo can create a different logical namespace.
Malformed percent escapes and whitespace in destination selectors are
rejected. One uniquely named SQLite spool is retained for each source until
transfer finishes, bounding preparation working memory while requiring temporary
disk proportional to all prepared sources. Spools preserve exact nodes,
deterministic source and node ordering, conflicts, and per-source reports.
They contain payload, result, and metadata content. Cleanup is attempted on
every exit. Cleanup failure can leave private artifacts and makes an otherwise
successful command fail. Combined failures retain their primary source or
destination attribution and add a fixed cleanup notice. Spool creation,
serialization, disk-write, finalization, corruption, and truncation fail before
destination access. If cleanup follows application, already accepted
destination writes remain.

Direct `redis://...#store-id` and `rediss://...#store-id` forms are legacy
source-only selectors that use the default `pollard` prefix. They can expose
credentials to process inspection and are rejected as destinations. Prefer
`redis-env:` for all Redis CLI use.
Sentinel and `client_factory` deployments remain Python API concerns, while
Redis Cluster remains outside the supported release matrix.

MongoDB uses the environment-only form
`mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id`:

```powershell
$env:POLLARD_MONGODB_URI = "mongodb://pollard_app:password@db-a.example,db-b.example/pollard?replicaSet=rs0&tls=true"
$mongoStore = "mongo-env:POLLARD_MONGODB_URI?database=pollard&prefix=pollard#support-prod"
pollard runs $mongoStore --json
pollard verify $mongoStore --json
pollard merge combined.db $mongoStore --json
pollard merge $mongoStore runs.db --json
$mongoArchive = "mongo-env:POLLARD_MONGODB_URI?database=pollard_archive&prefix=pollard_import#archive-2026-07"
pollard merge $mongoArchive runs.db --initialize-if-missing --json
```

The database, prefix, and store id default to `pollard`, `pollard`, and
`default` for sources; destinations must provide every value explicitly.
Sources and ordinary destinations validate an existing schema, coordinator,
and unique index without creating artifacts. Missing, partial, ambiguous, and
incompatibly indexed namespaces fail closed. Direct `mongodb://` and
`mongodb+srv://` arguments are rejected. MongoDB must still be a replica set or
sharded deployment. Index DDL is outside the logical initialization
transaction, so later initialization failure can leave an empty index-only
shell, but partial logical state is never repaired. MongoDB is not an import or
garbage-collection target. Advanced PyMongo client options remain a Python API
concern.

MongoDB traversal spans multiple snapshot transactions, and merge application
is node-by-node. Quiesce source and destination writers; concurrent writers can
race metadata updates. After a partial failure, rerun the exact merge, verify
the destination, and seal required roots.

Neo4j uses an environment-backed URI plus two required basic-auth references:

```powershell
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
$env:POLLARD_NEO4J_USER = "pollard_reader"
$env:POLLARD_NEO4J_PASSWORD = "<secret>"
$neo4jStore = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=neo4j#support-prod"
pollard runs $neo4jStore --json
pollard verify $neo4jStore --json
pollard merge combined.db $neo4jStore --json
pollard merge $neo4jStore runs.db --json
$neo4jArchive = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=pollard_archive#archive-2026-07"
pollard merge $neo4jArchive runs.db --initialize-if-missing --json
```

The grammar is
`neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`.
Database and store id default to `neo4j` and `default` for sources.
Destinations must provide the URI, user, and password environment references,
database, and store id explicitly. CLI labels omit both auth references and all
referenced values. Direct Neo4j and Bolt URI arguments are rejected.
Existing-only construction validates connectivity, schema, coordinator, the
two exact named constraints, and their owned online range indexes without
writes or DDL. It needs record and coordinator read privileges plus permission
to inspect constraints and indexes. Missing, partial, ambiguous, incompatible,
or offline state fails closed.

A Neo4j CLI command can span several managed transactions under read-committed
isolation and is not a command-wide snapshot. A merge destination is updated
node-by-node, not in one cross-node transaction. An exact rerun is idempotent,
but concurrent writers can race metadata. Quiesce source and destination
writers, inspect and rerun after failure, verify, and seal. Fresh initialization
creates or validates database-global shared constraints outside the logical
schema/coordinator transaction; do not delete them as automatic cleanup.
Partial state is not repaired. Neo4j is not an import or garbage-collection
target. The CLI supports basic auth only; authentication managers, custom
resolvers, client certificates, and advanced driver configuration remain
Python API concerns.

Kafka uses an environment-backed JSON client configuration and a required
topic selector:

```powershell
$env:POLLARD_KAFKA_CONFIG = '{"bootstrap.servers":"broker-1.example:9093,broker-2.example:9093","security.protocol":"SASL_SSL","sasl.mechanism":"SCRAM-SHA-512","sasl.username":"pollard_reader","sasl.password":"<secret>"}'
$kafkaStore = "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard-support-prod&timeout=120#support-prod"
pollard runs $kafkaStore --json
pollard verify $kafkaStore --json
pollard merge combined.db $kafkaStore --json
pollard merge $kafkaStore runs.db --json
```

The grammar is
`kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id`. Timeout defaults to
30 seconds and store id to `default` for sources; topic has no default. A
destination requires the explicit configuration reference, topic, and nonempty
store-id fragment. The JSON object must have unique keys and only string,
boolean, integer, or finite-number values. CLI labels include the variable,
topic, and store id but not timeout or any configuration value. Direct
`kafka://` broker specifications are rejected.

For sources, the CLI opens `KafkaStore(read_only=True)` without a producer and
freezes the fully validated topic prefix ending at the exclusive high watermark
captured during construction. All reads in one command therefore share a stable
in-memory view; reconnecting captures a newer prefix.

A destination must be an existing populated topic whose complete retained
history materializes at least one node and proves the explicit store id.
Missing, empty, wrong-identity, corrupt, truncated, or incompatibly configured
topics fail before producer construction and publish nothing. Pollard never
creates topics, so `--initialize-if-missing` remains invalid for Kafka. Topic
provisioning and the first valid seed node are operator/Python-writer actions.
The CLI finalizes and validates every source spool before destination
configuration lookup, then constructs the producer only after topic,
configuration, history, and prefix validation. It forces `acks=all`,
idempotence, deterministic operation ids, and replay confirmation.

Kafka merge application is append-by-append and non-atomic. Accepted events are
irreversible through Pollard, while an exact rerun publishes no new events.
Quiesce writers, inspect and rerun after a partial failure, then verify and seal.
Concurrent destination writers can race merge metadata. Kafka remains neither
an import nor a garbage-collection target, provides no shared arbitration or
record-level GC, and retains its linear cold-replay and memory-growth limits.
Callback authentication, custom logging, plugins, and other non-JSON client
configuration remain Python API concerns.

## Operations Boundary

- Use PostgreSQL for several hosts or sustained writer contention.
- Set the lease above expected database interruptions, renewal latency, and
  process scheduling stalls, and monitor renewal loss.
- Run garbage collection only during a coordinated offline maintenance window.
- Monitor database availability and capacity independently of Pollard.
- Do not claim fail-closed coordination between disconnected stores. Merge is
  an audit-ledger union after the fact, not a distributed budget protocol.
