# Observability

SQLite recordings are inspectable without a server, account, or network
connection. The core package installs a `pollard` command. `show`, `report`,
`verify`, `seal`, and `export` accept a SQLite path, PostgreSQL logical-store
specification, URL-backed Redis read specification, or environment-backed
MongoDB, Neo4j, or Kafka read specification. `runs` accepts the same sources,
while `merge` accepts all four as sources and accepts an environment-backed
Redis, MongoDB, Neo4j, or Kafka logical store as a destination. `import` and
`gc` remain SQLite-only. Every command has a `--json` form for scripts and CI.

## List and inspect runs

```powershell
pollard runs runs.db
pollard runs worker-a.db worker-b.db
pollard show runs.db <root-id>
pollard report runs.db <root-id>
```

`show` prints an ASCII tree by default so its output is encoding-safe on
Windows and Linux terminals. `--unicode` opts into Unicode connectors.

```text
root 2db812ec support-triage
\-- model_call 616b8aa2 gpt-deployment charges[steps=1 tokens=214]
    |-- tool_call 29b07977 lookup_customer charges[steps=1]
    \-- refusal c2fd7941 budget:tokens [REFUSED]
```

The default output includes structure, short node ids, labels, charges,
refusals, prune markers, and a `REDACTED` marker when a payload contains a
redaction digest. It does not include payloads or results. Use
`--payloads` only when the destination is allowed to receive prompt and result
content:

```powershell
pollard show runs.db <root-id> --payloads
pollard show runs.db <root-id> --json --payloads
```

## Static HTML

```powershell
pollard show runs.db <root-id> --html run.html
```

The export is one self-contained HTML file with native collapsible sections,
inline CSS, no JavaScript, and no remote assets. Payloads and results are absent
unless `--payloads` is also present. Pruned nodes are dimmed and refusals are
highlighted.

## Retention And Transfer

`export` accepts a SQLite path, PostgreSQL store specification, URL-backed
Redis read specification, or environment-backed MongoDB, Neo4j, or Kafka read
specification. `import` and `gc` remain offline SQLite-only operations:

```powershell
pollard export runs.db <root-id> subtree.json --json
pollard import subtree.json archive.db --json
pollard gc runs.db drop-pruned --json
pollard gc runs.db compact --json
```

Import verifies the complete subtree seal before writing nodes. Garbage
collection is explicit; `drop-pruned` removes marked subtrees and `compact`
reclaims unreferenced SQLite blobs. See [Data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md)
for field-level retention and redaction behavior.

Disconnected stores can be unioned without exporting an intermediate file:

```powershell
pollard merge combined.db worker-a.db worker-b.db --json
```

CLI merge validates only the destination selector and flag combination before
source preparation; it does not read destination environment values or
construct a destination client yet. It opens each source in order, fully
traverses and validates it, writes its exact nodes to a uniquely named SQLite
spool in a private temporary directory, closes the source, and validates the
finalized spool. Every spool must finish before destination configuration,
client construction, initialization, or writes. Preparation uses bounded
working memory but temporary disk proportional to the prepared data. Spools
contain payloads, results, and metadata, and Pollard attempts cleanup on every
exit. Cleanup failure can leave private artifacts and makes an otherwise
successful command fail. With another failure, the primary source or
destination attribution remains and a fixed cleanup notice is added. Creation,
serialization, disk-write, finalization, corruption, and truncation fail before
destination access. If cleanup fails after application, destination writes may
already exist and an exact rerun remains idempotent.

Use a credential-safe `pg-env:` reference for PostgreSQL inspection and export:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
pollard show "pg-env:POLLARD_PG_DSN#support-prod" <root-id>
pollard report "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --json
pollard verify "pg-env:POLLARD_PG_DSN#support-prod" --json
pollard seal "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --output seal.json --json
pollard export "pg-env:POLLARD_PG_DSN#support-prod" <root-id> subtree.json --json
```

Prefer `pg-env:` because a direct URI remains visible to process inspection.
For evidence-grade PostgreSQL verification, sealing, or export, quiesce writes
or otherwise keep the store stable for the full traversal. The CLI traversal
is not a repeatable-read snapshot.

PostgreSQL store syntax is documented in
[Scale-out stores and governance](https://github.com/jemsbhai/pollard/blob/main/docs/scale-out.md).

For an ordinary URL-backed Redis deployment, keep the URL in an environment
variable and add the exact key prefix and logical store id to the CLI spec:

```powershell
$env:POLLARD_REDIS_URL = "rediss://pollard_app:password@redis.example:6379/0"
$redisStore = "redis-env:POLLARD_REDIS_URL?prefix=pollard#support-prod"
$redisArchive = "redis-env:POLLARD_REDIS_URL?prefix=pollard-archive#existing-archive"
$redisNewArchive = "redis-env:POLLARD_REDIS_URL?prefix=pollard-import#archive-2026-07"
pollard runs $redisStore --json
pollard show $redisStore <root-id>
pollard report $redisStore <root-id> --json
pollard verify $redisStore --json
pollard seal $redisStore <root-id> --output seal.json --json
pollard export $redisStore <root-id> subtree.json --json
pollard merge combined.db $redisStore --json
pollard merge $redisArchive runs.db --replay --json
pollard merge $redisNewArchive runs.db --replay --initialize-if-missing --json
```

Redis sources open with `create=False`, require an existing identity, schema,
and revision, and do not initialize a missing prefix/store-id combination. A
source typo therefore fails closed. Its multi-read traversal is not a stable
snapshot under concurrent writes, so quiesce writes for evidence-grade
verification, sealing, or export.

A Redis destination must use `redis-env:` and explicitly supply
`?prefix=PREFIX` and `#store-id`. It opens with `create=False` by default, so a
missing or mistyped namespace fails closed. `--initialize-if-missing` is valid
for Redis only with an explicit `redis-env:` destination. With that opt-in, and
only after every source spool has been finalized and validated, the CLI uses
`create=True` and atomically initializes identity, schema, and revision for a
missing namespace; an existing destination must match them. Review both
selectors before opting in because a typo can then create a different
namespace. Malformed percent escapes and whitespace in destination selectors
are rejected.

Merge copies nodes and metadata only and is not a cross-node or cross-backend
transaction, so a failed copy can leave accepted changes. An exact rerun is
idempotent. `import` and `gc` remain SQLite-only.

Direct `redis://...#store-id` and `rediss://...#store-id` forms remain legacy
sources that select the default `pollard` prefix. They can expose credentials
to process inspection and are rejected as destinations; prefer `redis-env:`.

`redis-env:` constructs a standard URL-backed client. Sentinel discovery and
caller-owned `client_factory` configuration remain Python API concerns, and
Redis Cluster remains outside the supported release matrix.

For MongoDB, place the URI in an environment variable and select the exact
database, collection prefix, and logical store id separately:

```powershell
$env:POLLARD_MONGODB_URI = "mongodb://pollard_app:password@db-a.example,db-b.example/pollard?replicaSet=rs0&tls=true"
$mongoStore = "mongo-env:POLLARD_MONGODB_URI?database=pollard&prefix=pollard#support-prod"
pollard runs $mongoStore --json
pollard show $mongoStore <root-id>
pollard report $mongoStore <root-id> --json
pollard verify $mongoStore --json
pollard seal $mongoStore <root-id> --output seal.json --json
pollard export $mongoStore <root-id> subtree.json --json
pollard merge combined.db $mongoStore --json
pollard merge $mongoStore runs.db --json
$mongoNewArchive = "mongo-env:POLLARD_MONGODB_URI?database=pollard_archive&prefix=pollard_import#archive-2026-07"
pollard merge $mongoNewArchive runs.db --initialize-if-missing --json
```

The grammar is
`mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id`; omitted values
default to `pollard`, `pollard`, and `default` for sources. Destinations must
spell out every value. The selector values must match the writer exactly and
the database is not inferred from the URI path. CLI output labels the
environment variable and selector values, never the URI.
Direct `mongodb://` and `mongodb+srv://` arguments are rejected because a URI
on the command line can expose credentials to process inspection and shell
history.

MongoDB sources and ordinary destinations validate the topology, exact schema,
coordinator, and unique index without creating any artifact. A standalone
server and an unused, partial, ambiguous, or incompatibly indexed namespace
fail closed. `--initialize-if-missing` opts into fresh destination creation
only after all source spools are finalized and validated. MongoDB creates
the physical unique index before atomically initializing schema and coordinator
revision; index DDL is outside that logical transaction and can leave an empty
index-only shell after later failure. Pollard never repairs partial logical
state implicitly.

MongoDB is a merge source and destination, but not an import or
garbage-collection target. A CLI walk spans multiple MongoDB snapshot
transactions, and destination application is node-by-node and non-atomic.
Quiesce source and destination writers, repeat the exact merge after partial
failure, then verify and seal. Concurrent destination writers can race metadata
updates. Stable API objects, caller-supplied CA files, compressors, application
names, and other client options that cannot be represented in the URI remain
Python API concerns.

For Neo4j, keep the URI, user, and password in separate environment variables:

```powershell
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
$env:POLLARD_NEO4J_USER = "pollard_reader"
$env:POLLARD_NEO4J_PASSWORD = "<secret>"
$neo4jStore = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=neo4j#support-prod"
pollard runs $neo4jStore --json
pollard show $neo4jStore <root-id>
pollard report $neo4jStore <root-id> --json
pollard verify $neo4jStore --json
pollard seal $neo4jStore <root-id> --output seal.json --json
pollard export $neo4jStore <root-id> subtree.json --json
pollard merge combined.db $neo4jStore --json
pollard merge $neo4jStore runs.db --json
$neo4jArchive = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=pollard_archive#archive-2026-07"
pollard merge $neo4jArchive runs.db --initialize-if-missing --json
```

The grammar is
`neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`.
Both auth references are required. Database and store id default to `neo4j` and
`default` for sources; both must match the writer configuration. A destination
must explicitly provide all three environment references, database, and store
id. CLI output labels the URI reference, database, and store id, but omits both
auth references and all three environment values. Direct `neo4j://`,
`neo4j+s://`, `neo4j+ssc://`, `bolt://`, `bolt+s://`, and `bolt+ssc://`
arguments are rejected.

Sources and destinations are existing-only by default. Construction validates
the exact schema and coordinator, two named database-wide uniqueness
constraints, and their owned online range indexes without writes or constraint
DDL. Missing, partial, ambiguous, incompatible, or offline state fails closed.
`--initialize-if-missing` is the only explicit
fresh-namespace opt-in and is applied only after all sources are fully
spooled and validated. Fresh initialization creates or validates the
shared global constraints outside the logical transaction, then creates schema
and coordinator together in one managed transaction. Constraints may serve
other store ids and must not be removed as automatic cleanup.

Sessions use write routing to reach a primary. Neo4j's default isolation is
read committed, and a CLI command can span multiple managed transactions; it
is not a command-wide snapshot. Destination application is node-by-node and
non-atomic. Quiesce source and destination writers, rerun an exact failed merge
idempotently, verify, and seal; concurrent writers can race merge metadata.

Neo4j can be an observational/read or merge source and an environment-backed
merge destination; it is not an import or garbage-collection target. This CLI
path supports basic auth only. Authentication managers, bearer or Kerberos
auth, custom resolvers, client certificates, and advanced driver configuration
remain Python API concerns.

For Kafka, put the complete scalar confluent-kafka configuration mapping in one
environment variable and select the dedicated topic separately:

```powershell
$env:POLLARD_KAFKA_CONFIG = '{"bootstrap.servers":"broker-1.example:9093,broker-2.example:9093","security.protocol":"SASL_SSL","sasl.mechanism":"SCRAM-SHA-512","sasl.username":"pollard_reader","sasl.password":"<secret>"}'
$kafkaStore = "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard-support-prod&timeout=120#support-prod"
pollard runs $kafkaStore --json
pollard show $kafkaStore <root-id>
pollard report $kafkaStore <root-id> --json
pollard verify $kafkaStore --json
pollard seal $kafkaStore <root-id> --output seal.json --json
pollard export $kafkaStore <root-id> subtree.json --json
pollard merge combined.db $kafkaStore --json
pollard merge $kafkaStore runs.db --json
```

The grammar is
`kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id`. Topic is required;
timeout is a positive integer and defaults to `30`, while store id defaults to
`default` for sources. A destination must explicitly provide the configuration
reference, topic, and a nonempty store-id fragment. The environment value must
be a JSON object with unique property names and only string, boolean, integer,
or finite-number values. Null, arrays, nested objects, non-finite numbers, and
`plugin.library.paths` are rejected. The CLI removes `debug` and `aws_debug`,
fixes `log_level=0`, and never places the configuration value in its output.
Its safe label includes only the configuration-variable name, topic, and store
id; timeout is an operational setting rather than store identity. Direct
`kafka://` arguments are rejected.

Source construction uses `KafkaStore(read_only=True)`, creates no producer, and
validates the exact existing single-partition, infinite-retention topic. It
replays committed events from offset zero through the exclusive high watermark
observed during construction and then freezes that in-memory prefix for every
read in the command. Appends after that boundary are not visible; `reconnect()`
captures a new prefix. This makes verification, sealing, and export internally
consistent without stopping writers. Quiesce writers only when the result must
represent a drained final topic, and then record the topic, partition zero, and
exclusive high watermark independently because Pollard seals do not cover
Kafka offsets.

A Kafka destination is existing-and-populated only. Full replay must
materialize at least one node and prove the explicit store id. Missing, empty,
wrong-identity, corrupt, truncated, or incompatibly configured topics fail
before producer construction and publish nothing. `--initialize-if-missing` is
invalid: Pollard never creates topics. An operator must provision the topic, and
a reviewed Python writer must seed its first valid node.

Every source spool is finalized and validated before the CLI reads the
destination configuration or opens it. Only after topic, configuration,
history, and prefix validation does Pollard create the destination producer with
`acks=all`,
idempotence, deterministic operation ids, and replay confirmation. Merge
appends commands one at a time and is not atomic; accepted events are
irreversible through Pollard, while an exact rerun publishes no new events.
Quiesce writers, rerun after partial failure, verify, and seal. Kafka is not an
import or garbage-collection target and still provides neither shared budget
arbitration nor record-level GC. Cold replay is linear in retained history and
must finish within the selected timeout. Callback-based authentication, custom
loggers, plugins, and other non-JSON client configuration remain Python API
concerns.

## Integrity and seals

Verify one run or every root in a store:

```powershell
pollard verify runs.db <root-id>
pollard verify runs.db
pollard verify runs.db --json
```

The exit code is `0` for a clean recording, `1` when integrity findings exist,
and `2` for an invalid command or unreadable input. This makes verification
usable as a CI step.

Create a rolling subtree seal and optionally write the full report:

```powershell
pollard seal runs.db <root-id>
pollard seal runs.db <root-id> --output seal.json --json
```

## Charge reports

`pollard report` sums stored charges for a run or subtree. Hybrid hits also
accumulate avoided charges in mutable node metadata, so later CLI reports can
show historical avoided work. Pure replay stays read-only; its avoided charges
remain available from `run.report()` during that process. Mutable metadata is
excluded from the seal by design.

Live revalidation adds a sibling model-call observation and a child comparison
note beneath the golden node's parent. The observation's actual charges are
included in reports. Default `show` output exposes only node topology, labels,
and accounting; `--payloads` also reveals the retained golden and live payloads,
results, and value-free comparison evidence.

Read-only replay covers roots, notes, branch anchors, results, registry
bindings, refusals, and prune metadata. Constructing a replay `Runtime` from a
SQLite path also opens the database with SQLite query-only access. Passing an
already-open custom store relies on that store's own connection mode, while the
runtime still issues no replay writes.

## OpenTelemetry

Install the bridge and configure any OpenTelemetry SDK and exporter your
application already uses:

```powershell
pip install "pollard[otel]" opentelemetry-sdk
```

Offline export preserves the Pollard tree as OpenTelemetry parent-child spans:

```python
from opentelemetry import trace
from pollard import SQLiteStore
from pollard.otel import export_spans

with SQLiteStore("runs.db") as store:
    count = export_spans(store, root_id, trace.get_tracer("my-agent"))
```

For spans as new nodes are recorded, pass the optional runtime callback:

```python
from opentelemetry import trace
from pollard import Runtime
from pollard.otel import live_span_hook

runtime = Runtime("runs.db", on_node=live_span_hook(trace.get_tracer("my-agent")))
```

The offline bridge should be preferred when exact span topology matters. A live
child may be recorded after its parent's live span ended, so the live bridge
uses `pollard.parent.id` for that relationship. A live callback failure emits a
runtime warning after the node is safely stored; telemetry failure does not
discard or interrupt the governed result.

The bridge emits current GenAI semantic-convention attributes where Pollard has
the required data, including `gen_ai.operation.name`, provider, request and
response model, and input and output token usage. Pollard-specific fields cover
node identity, kind, attempt, charges, avoided work, refusal reason, registry
digest, prune state, and result digest. See the OpenTelemetry
[GenAI attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).

Prompt, result, tool arguments, and tool outputs are never placed on spans by
this bridge. The tree keeps those values in the selected Pollard store; the
telemetry export carries structure and accounting only.
