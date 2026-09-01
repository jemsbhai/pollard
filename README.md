# pollard

Governed execution trees for AI agents: budget it, gate it, replay it.

## 90-Second Credential-Free Start

Pollard requires Python 3.10 or newer. The core package has no runtime
dependencies, and this first run needs no model-provider account or API key.

On POSIX systems, including macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pollard
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install pollard
```

Save this as `first_run.py`:

```python
from pollard import Budget, Runtime

def call_model(payload):
    return {
        "text": f"offline reply for {payload['prompt']}",
        "usage": {"input_tokens": 2, "output_tokens": 4},
    }

with Runtime().run("first-run", budget=Budget(tokens=10, steps=1)) as run:
    node = run.model_call(
        {"model": "local-demo", "prompt": "hello"},
        fn=call_model,
    )
    spent = run.report()["spent"]
    print(node.result["text"])
    print(f"tokens: {spent['tokens']:.0f}; steps: {spent['steps']:.0f}")
```

Run it on POSIX or macOS:

```sh
.venv/bin/python first_run.py
```

Or on Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe first_run.py
```

Expected output:

```text
offline reply for hello
tokens: 6; steps: 1
```

`Runtime()` uses an in-memory store, so this example creates no database file.
The deterministic `call_model` callable stands in for any caller-owned model
client, and its usage fields let Pollard settle the token and step ledger. This
exact program's root ID is
`fb4f2a23cc196e53f0fa800a71c025e0a9b7ac5890b83c4d9d1a0214175d9dd5`, and
its model-call ID is
`c4882b75addd9867f623049798e2c6cebc3d49daa80bd5a825c102cf0580fd30`.
The installed-wheel smoke test locks both values so Linux, Windows, and macOS CI
detect platform-dependent identity drift.

## What Pollard Does

Pollard is a runtime primitive, not an agent framework. It records each step as
a node in a content-addressed tree. Node identity is a hash of the step inputs,
parent identity, kind, and attempt number, so the tree gives you a control-flow
ledger without owning your model client, tools, prompts, or loop.

What you get:

- Budget: refuse a step before it runs when a known budget would be exceeded.
- Branch and rollback: make alternate children, move the cursor back, and keep shared history.
- Audit: each node id commits to its ancestry and identity payload.
- Registry firewall: registered tool calls resolve against a versioned action set or fail closed.
- Replay: record semantic steps once, then serve stored results in tests and CI.
- Revalidation: compare a recording with a separately stored, budgeted live
  observation without replacing the golden result.
- Scale-out: share atomic budgets and sliding windows across workers through
  SQLite, PostgreSQL, Redis, MongoDB, or Neo4j, and merge disconnected stores
  later. Kafka provides ordered audit and replay storage without shared limits.

## First Live Provider Integration

Install the OpenAI adapter with the same virtual environment when you are ready
to replace the local callable.

On POSIX or macOS:

```sh
.venv/bin/python -m pip install "pollard[openai]"
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install "pollard[openai]"
```

```python
from openai import OpenAI
from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn

client = OpenAI(max_retries=0)
runtime = Runtime("runs.db", refuse_duplicate_recordings=True)
with runtime.run("triage", budget=Budget(tokens=20_000)) as run:
    node = run.model_call(
        {"model": "gpt-5.6", "input": "Summarize: ...", "max_output_tokens": 256},
        fn=make_responses_fn(client, store=False),
    )
    print(node.result["text"], run.report())
```

This path makes a provider request and can incur charges. The client belongs to
your code: Pollard does not read credentials or construct provider clients.
The opt-in duplicate guard prevents a sequential rerun of the same recorded
identity from dispatching again; use an explicit new `attempt` for a genuine
retry. It is not a distributed lock, so concurrent callers still need provider
idempotency when exactly-once dispatch matters.
Anthropic, Amazon Bedrock, and LiteLLM adapters follow the same pattern through
`pollard[anthropic]`, `pollard[bedrock]`, and `pollard[litellm]`. Azure OpenAI
uses the OpenAI adapter with an Azure-configured client. See
[Cloud-hosted model providers](https://github.com/jemsbhai/pollard/blob/main/docs/cloud-providers.md)
for direct AWS and Azure examples plus Vertex AI and other LiteLLM routes.

Budget semantics are honest about what can be controlled. If a precheck estimate proves a step would exceed budget, pollard records a refusal node and does not call your function. If the actual result charge exceeds budget after the function returns, that node still stands because the spend already happened; later steps are refused. Transactional stores reserve estimated charges before execution and settle actual charges afterward, so exact step and request prechecks stay within one shared limit under concurrent writers.

Current limits:

- Strict replay deterministically serves the recorded output and never
  resamples the provider. Explicit live revalidation can detect observable
  drift, but cannot guarantee that a hosted provider reproduces a prior sample.
- Hosted API energy use is not measured. The NVML energy meter is for local GPU inference only.
- SQLite serializes writers on one host. PostgreSQL, Redis, MongoDB, and Neo4j
  can coordinate worker teams when every worker uses the same backend and
  logical store id.
- Kafka is an append-only Store, not a shared budget arbiter. It requires a
  dedicated single-partition topic with infinite retention and no compaction.
- HashRopeStore is an in-process operation-log backend, not a multi-writer
  database. Explicit offline garbage collection rewrites its snapshot.
- `TokenmasterMeter` can apply a caller-supplied prompt estimator before
  dispatch. Per-request profile checks are opt-in and are separate from the
  cumulative `Budget(tokens=...)` ledger. A post-response limit diagnostic
  records what happened but cannot undo a provider call that already completed.
- `TokenmasterCostMeter` uses tokenmaster's tiered request pricing. Its
  preflight estimate is conservative, and it enforces dollars only when the
  run supplies `Budget(usd=...)`; the provider's bill remains authoritative.
- Prompt estimators are approximations. Images, tool schemas, provider-added
  instructions, and wire-format changes can make the settled usage differ.
- Shared arbitration requires every worker to use the same transactional store
  and logical store id. Pollard does not provide decentralized consensus.
- The audit tree is tamper-evident, not tamper-proof. Verification detects changed history, but it cannot stop deletion of the whole store file.

## Installation And Integration Paths

Pollard requires Python 3.10 or newer. The core package has no runtime
dependency and includes in-memory and SQLite stores, the registry firewall,
budgets, replay, seals, merge, governance helpers, and a CLI with offline
SQLite inspection:

```powershell
python -m pip install pollard
```

Install only the integrations used by the application:

| Need | Install | Boundary |
|---|---|---|
| OpenAI API or Azure OpenAI v1 with an API key | `pollard[openai]` | Responses and Chat Completions, sync or async, streaming or complete |
| Azure OpenAI v1 with Microsoft Entra ID | `pollard[azure-openai]` | OpenAI adapter plus the Azure Identity token provider |
| Anthropic API | `pollard[anthropic]` | Messages, sync or async, streaming or complete, optional live token count |
| Amazon Bedrock | `pollard[bedrock]` | Converse and ConverseStream, optional CountTokens precheck |
| Other cloud providers | `pollard[litellm]` | LiteLLM chat routes for Vertex AI, Azure AI, SageMaker, OCI, Watsonx, Databricks, and others |
| LangGraph | `pollard[langgraph]` | OpenAI-backed LangGraph node integration recipe |
| LangChain | `pollard[langchain]` | Offline incident-response and support-policy RAG workflows with typed output and replay; optional OpenAI live modes |
| Pydantic models | `pollard[pydantic]` | Model-generated registry schema with dry-run, confirmation, redaction, and replay |
| pydantic-ai | `pollard[pydantic-ai]` | Typed claim agent with governed model requests and registered tool execution |
| MCP | `pollard[mcp]` | Discover MCP tools and expose them through a Pollard registry |
| Shared PostgreSQL store | `pollard[pg]` | Transactional multi-process and multi-host coordination |
| Shared Redis store | `pollard[redis]` | Exact shared coordination with Redis persistence and no-eviction prerequisites |
| Shared MongoDB store | `pollard[mongodb]` | Exact shared coordination on a replica set or sharded deployment |
| Kafka event store | `pollard[kafka]` | Ordered audit and replay on one dedicated topic; no shared arbitration |
| Shared Neo4j store | `pollard[neo4j]` | Exact shared coordination through primary-routed graph transactions |
| Every remote store driver | `pollard[stores]` | PostgreSQL, Redis, MongoDB, Kafka, and Neo4j drivers |
| OpenTelemetry | `pollard[otel]` | Content-free live or offline span export |
| OpenAI prompt estimate | `pollard[estimate-openai]` | Local tiktoken estimate plus an explicit output reservation |
| Local GPU energy | `pollard[nvml]` | Whole-GPU NVML measurement for supported local hardware |
| Hashrope store | `pollard[hashrope]` | In-process append-only operation-log backend |
| tokenmaster governance | `pollard[tokenmaster]` | profile limits, state and advice, and tier-aware USD accounting |

Pollard itself needs no model-provider credential. A live recipe uses the
credential chain of its caller-owned SDK. The complete provider, endpoint,
model, IAM, cost, and data-retention boundaries are in
[Cloud-hosted model providers](https://github.com/jemsbhai/pollard/blob/main/docs/cloud-providers.md)
and the runnable [integration recipes](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md).
The LangChain incident-response, LangChain support-policy RAG, Pydantic refund,
and pydantic-ai claim-triage recipes use deterministic local paths by default.
Only an explicit `--live` flag on either LangChain recipe or the pydantic-ai
recipe enables hosted inference.

## Streaming And Estimates

A model function may return a result dictionary or an iterator of chunk
dictionaries. `model_call(..., on_delta=callback)` forwards chunks in order.
With `keep_chunks=True`, Pollard stores those chunks under `result["chunks"]`
and re-emits them through the callback during replay. Charges settle once, after
the stream ends, and node identity remains a function of the input payload.

`TokenMeter(estimator=..., reserved_output_tokens=N)` applies an estimated input
charge plus an explicit output reservation at precheck. A refusal caused by that
estimate records `{"estimated": "true"}`. The settled provider usage remains the
source of actual token charges when it is valid. If usage is missing or invalid,
Pollard keeps the available precheck estimate as a conservative charge and
records the fallback in node metadata.

The optional tiktoken estimator is available as:

```python
from pollard.estimators.openai import OpenAITokenEstimator
from pollard.meters import TokenMeter

meter = TokenMeter(OpenAITokenEstimator(), reserved_output_tokens=1024)
```

See the [recipe collection](https://github.com/jemsbhai/pollard/tree/main/docs/recipes)
for full tool loops and integration patterns.

## Shared Budgets And Rate Windows

Install the PostgreSQL extra and keep the DSN in an environment variable:

```powershell
pip install "pollard[pg]"
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
```

```python
import os

from pollard import Budget, PostgresStore, Runtime, WindowMeter
from pollard.meters import StepMeter

store = PostgresStore(os.environ["POLLARD_PG_DSN"], store_id="support-prod")
runtime = Runtime(
    store,
    meters=[StepMeter(), WindowMeter("requests", 60, 60)],
)
with runtime.run("triage", budget=Budget(steps=1_000)) as run:
    run.model_call({"model": "mock"}, fn=lambda _payload: {"text": "ready"})
```

The database role needs normal read and write access plus permission to create
Pollard's tables and indexes on first use. Existing unversioned schemas require
an explicit backed-up, drained migration and unknown versions are refused. It
does not need OpenAI, Anthropic, AWS, Azure, or other model credentials. See
[Scale-out stores and governance](https://github.com/jemsbhai/pollard/blob/main/docs/scale-out.md)
for shared limits and [PostgreSQL operations](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md)
for schema migration, backup, restore, lease renewal, and reconnect procedures.
Redis, MongoDB, and Neo4j use the same runtime reservation contract. Their
connection examples, durability requirements, schema behavior, and recovery
limits are in [Distributed store operations](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md).
Dedicated [Redis](https://github.com/jemsbhai/pollard/blob/main/docs/redis-operations.md),
[MongoDB](https://github.com/jemsbhai/pollard/blob/main/docs/mongodb-operations.md),
[Neo4j](https://github.com/jemsbhai/pollard/blob/main/docs/neo4j-operations.md),
and [Kafka](https://github.com/jemsbhai/pollard/blob/main/docs/kafka-operations.md)
guides cover topology, least privilege, monitoring, failover acceptance, and
recovery.
The configured
[`09_distributed_stores.py`](https://github.com/jemsbhai/pollard/blob/main/examples/09_distributed_stores.py)
walkthrough records, strictly replays, verifies, and seals one deterministic
run against any of the five remote backends without contacting a model
provider.

## Observability

The core package includes a CLI that remains fully offline for SQLite
recordings. `show`, `report`, `verify`, `seal`, and `export` accept SQLite
recording paths, PostgreSQL store specs, URL-backed Redis read specs, or
environment-backed MongoDB, Neo4j, or Kafka read specs. `runs` accepts the same
sources. `merge` accepts Redis, MongoDB, Neo4j, and Kafka sources, and accepts
environment-backed Redis, MongoDB, Neo4j, and Kafka logical stores as
destinations:

```powershell
pollard runs runs.db
pollard runs team-a.db team-b.db
pollard merge combined.db team-a.db team-b.db
pollard show runs.db <root-id>
pollard report runs.db <root-id> --json
pollard verify runs.db
pollard seal runs.db <root-id> --output seal.json --json
pollard export runs.db <root-id> subtree.json --json
pollard show runs.db <root-id> --html run.html
```

Keep a PostgreSQL DSN out of process arguments by using a `pg-env:` reference:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
pollard show "pg-env:POLLARD_PG_DSN#support-prod" <root-id>
pollard report "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --json
pollard verify "pg-env:POLLARD_PG_DSN#support-prod" --json
pollard seal "pg-env:POLLARD_PG_DSN#support-prod" <root-id> --output seal.json --json
pollard export "pg-env:POLLARD_PG_DSN#support-prod" <root-id> subtree.json --json
```

Use `redis-env:VARIABLE?prefix=PREFIX#store-id` for a credential-safe Redis
source or merge destination without placing its URL in the process arguments:

```powershell
$env:POLLARD_REDIS_URL = "rediss://pollard_app:password@redis.example:6379/0"
pollard show "redis-env:POLLARD_REDIS_URL?prefix=pollard#support-prod" <root-id>
pollard verify "redis-env:POLLARD_REDIS_URL?prefix=pollard#support-prod" --json
pollard merge combined.db "redis-env:POLLARD_REDIS_URL?prefix=pollard#support-prod" --json
pollard merge "redis-env:POLLARD_REDIS_URL?prefix=pollard-archive#existing-archive" runs.db --json
pollard merge "redis-env:POLLARD_REDIS_URL?prefix=pollard-import#archive-2026-07" runs.db --initialize-if-missing --json
```

Direct `redis://...#store-id` and `rediss://...#store-id` forms are legacy
source-only forms that use the default `pollard` prefix and can expose
credentials to process inspection. They are rejected as merge destinations;
use `redis-env:` for every new command and for all Redis destinations. A source
opens with `create=False`, so an unused or mistyped prefix/store-id combination
fails closed. Every Redis destination must explicitly include both
`?prefix=PREFIX` and `#store-id`. By default it also opens with `create=False`,
so a missing or mistyped namespace fails closed.

Pass `--initialize-if-missing` to opt into creating a missing destination.
That flag is valid only with an explicit `redis-env:`, `mongo-env:`, or
`neo4j-env:` destination. Pollard may validate the destination selector's
syntax and eligibility first, but does not read its environment variables or
construct a destination client at that stage. After every source has been
opened, fully traversed, validated, serialized to a private disk-backed spool,
closed, and the completed spool has passed an independent integrity check,
Pollard uses
`create=True`. Redis atomically initializes identity, schema, and revision in
one Redis transaction; MongoDB's distinct physical-index and logical-state
boundaries and Neo4j's separate global-constraint and logical-state boundaries
are described below. Because this opt-in can turn a destination typo into a
different logical namespace, review every selector before using it. Malformed
percent escapes and ambiguous whitespace selectors are rejected. Kafka
destinations never accept this flag: topics are provisioned by an operator, and
the CLI requires an existing populated topic whose retained Pollard history
proves its store identity.

Merge copies nodes and metadata, not active budget, window, reservation, or
lease state. Every source is fully traversed before the destination
configuration is resolved or the destination is opened,
but destination application is node-by-node rather than one cross-node or
cross-backend transaction. An error can leave nodes already accepted by the
destination; repeating the exact merge is idempotent. Quiesce source and
destination writers, then verify and seal the result. Concurrent destination
writers can race metadata updates. Redis, MongoDB, and Neo4j source traversal
are not stable whole-store snapshots during concurrent writes.

CLI merge keeps only a bounded preparation working set in RAM and stores each
complete prepared source in its own uniquely named SQLite spool inside a
private temporary directory. The spools preserve exact node fields,
deterministic source and node ordering, result and metadata conflict behavior,
and per-source JSON reports. They contain retained payloads, results, and
metadata, so the temporary filesystem needs appropriate access controls and
enough free space for all sources. Pollard attempts to remove the private
directory on every exit. A cleanup failure can leave sensitive private
artifacts and makes an otherwise successful command fail; when another failure
already exists, the error preserves its source or destination attribution and
adds a fixed cleanup-failure notice. Creation, serialization, disk-write,
finalization, corruption, or truncation errors before spool finalization do not
access the destination. A cleanup error after application does not roll back
destination writes, so inspect and rerun the exact merge.
`import` and `gc` remain SQLite-only. Sentinel or `client_factory` construction
remains a Python API concern, while Redis Cluster remains outside the supported
release matrix.

Use `mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id` for a MongoDB
source or merge destination:

```powershell
$env:POLLARD_MONGODB_URI = "mongodb://pollard_app:password@db-a.example,db-b.example/pollard?replicaSet=rs0&tls=true"
$mongoStore = "mongo-env:POLLARD_MONGODB_URI?database=pollard&prefix=pollard#support-prod"
pollard show $mongoStore <root-id>
pollard verify $mongoStore --json
pollard merge combined.db $mongoStore --json
pollard merge $mongoStore runs.db --json
$mongoArchive = "mongo-env:POLLARD_MONGODB_URI?database=pollard_archive&prefix=pollard_import#archive-2026-07"
pollard merge $mongoArchive runs.db --initialize-if-missing --json
```

The defaults are database `pollard`, collection prefix `pollard`, and store id
`default` for sources. A destination must spell out all three values and is
existing-only by default. `--initialize-if-missing` is the explicit creation
opt-in. Existing-only construction creates no collections, indexes,
coordinator state, or schema records. Fresh creation establishes the unique
record index first, then initializes schema and coordinator revision together
in one MongoDB transaction. Because MongoDB index DDL is outside that logical
transaction, an index-only empty shell can remain after a later construction
failure; it is safe to inspect and retry, but partial logical or incompatible
index states are never repaired implicitly. Only `mongo-env:` is accepted;
direct `mongodb://` and `mongodb+srv://` arguments are rejected.

The deployment must still be a replica set or mongos. MongoDB CLI traversal
spans multiple snapshot transactions and is not one point-in-time snapshot.
Quiesce source and destination writers for evidence-grade transfer, verify the
destination after success or partial failure, and seal required roots. MongoDB
is not an import or garbage-collection target. Client options that cannot be
expressed in the URI remain a Python API concern.

Use `neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`
to provide a URI and basic-auth values without placing them in process
arguments:

```powershell
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
$env:POLLARD_NEO4J_USER = "pollard_reader"
$env:POLLARD_NEO4J_PASSWORD = "<secret>"
$neo4jStore = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=neo4j#support-prod"
pollard show $neo4jStore <root-id>
pollard verify $neo4jStore --json
pollard merge combined.db $neo4jStore --json
pollard merge $neo4jStore runs.db --json
$neo4jArchive = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=pollard_archive#archive-2026-07"
pollard merge $neo4jArchive runs.db --initialize-if-missing --json
```

Both auth references are required. Database and store id default to `neo4j`
and `default` for sources and must match the writer. A destination must spell
out the URI, user, and password environment references, database, and store id,
and is existing-only by default. CLI labels omit both auth references and
never include their values. Direct Neo4j and Bolt URI arguments are rejected.

Existing-only construction validates the exact Pollard schema, coordinator,
two named database-wide uniqueness constraints, and their owned online range
indexes without writes or constraint DDL. `--initialize-if-missing` is the
explicit fresh-namespace opt-in. For a
fresh logical namespace, Pollard creates or validates the shared global
constraints outside the logical transaction, then initializes coordinator and
schema together in one managed transaction. Missing, incompatible, or offline
constraint/index state and every partial or ambiguous logical state fail closed
without implicit repair. Because constraints and their indexes are shared by all Pollard
stores in the database, use a dedicated database and appropriately privileged
role when initialization must create them.

Reads remain write-routed to a primary. A command can span several
read-committed transactions and is not a command-wide snapshot. Neo4j
destination application is node-by-node rather than one transaction; an error
can leave accepted nodes, and an exact rerun is idempotent. Quiesce source and
destination writers, verify the result, and seal required roots. Concurrent
destination writers can race merge metadata updates. Neo4j is not an import or
garbage-collection target. The CLI supports basic auth only; authentication
managers, custom resolvers, client certificates, and advanced driver
configuration remain Python API concerns.

Use `kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id` to inspect or
merge into one pre-provisioned Kafka topic through an environment-backed
confluent-kafka configuration:

```powershell
$env:POLLARD_KAFKA_CONFIG = '{"bootstrap.servers":"broker-1.example:9093,broker-2.example:9093","security.protocol":"SASL_SSL","sasl.mechanism":"SCRAM-SHA-512","sasl.username":"pollard_reader","sasl.password":"<secret>"}'
$kafkaStore = "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard-support-prod&timeout=120#support-prod"
pollard runs $kafkaStore --json
pollard verify $kafkaStore --json
pollard export $kafkaStore <root-id> subtree.json --json
pollard merge combined.db $kafkaStore --json
pollard merge $kafkaStore runs.db --json
```

The topic is required, timeout defaults to 30 seconds, and store id defaults to
`default` for sources. A destination must explicitly provide the configuration
environment reference, topic, and a nonempty `#store-id` fragment. The
environment value is a JSON object whose values are strings, booleans, integers,
or finite numbers; duplicate keys and nested values are rejected. CLI labels
include the configuration reference, topic, and store id, but omit the timeout
and every referenced configuration value. Direct `kafka://` arguments are
rejected.

Kafka CLI source access uses `KafkaStore(read_only=True)`: it creates no
producer, validates the existing topic, and freezes the complete committed
prefix from offset zero through the exclusive high watermark observed during
construction. Every read in that command uses the same in-memory view;
reconnecting captures a new prefix. A destination is existing-and-populated
only: full replay must
materialize at least one node and prove that every retained event belongs to
the explicit store id. Missing, empty, wrong-identity, corrupt, truncated, or
incompatibly configured topics fail before producer construction and publish
nothing. Pollard never creates topics, and `--initialize-if-missing` is invalid
for Kafka; seed the first valid node through a reviewed Python writer after an
operator provisions the topic.

Only after all source spools are finalized and validated does the CLI read the
destination configuration. After the destination topic, configuration, history,
and prefix validate, it constructs the producer. KafkaStore
forces `acks=all`, idempotence, deterministic operation ids, and replay
confirmation. Merge still appends node and metadata commands one at a time, so
a failure can leave irreversible accepted events. An exact rerun publishes no
new events. Quiesce writers, inspect and rerun after failure, then verify and
seal. Kafka remains neither an import nor a garbage-collection target, has no
shared budget arbitration or record-level GC, and retains its linear cold
replay and memory-growth limits. Callback-based authentication and other
non-JSON client configuration remain Python API concerns.

`show` defaults to an ASCII, content-free tree. Payloads and results require an
explicit `--payloads` flag. The HTML export is one static file with no remote
assets. The optional `pollard[otel]` bridge exports the same node topology to a
caller-configured OpenTelemetry tracer without placing prompt or result content
on spans. See [Observability](https://github.com/jemsbhai/pollard/blob/main/docs/observability.md)
for CLI exit codes, JSON forms, seals, HTML, and OpenTelemetry examples.

## Storage And Data Governance

`SQLiteStore` and `PostgresStore` transparently intern repeated payload strings
of at least 1 KiB. Interning changes only the storage encoding. Callers receive
the original payload, and node ids are identical with interning on or off.

Redaction is separate. `redact(value, hint=None)` replaces a value before node
identity is computed, so the plaintext never reaches a Pollard store. Registry
schemas can apply the same rule automatically:

```python
from pollard import ActionSpec

def send_message(_args):
    return {"queued": True}

spec = ActionSpec(
    "send",
    "1",
    "Send a message.",
    {
        "type": "object",
        "properties": {"token": {"type": "string", "sensitive": True}},
        "required": ["token"],
    },
    True,
    handler=send_message,
)
```

The handler receives the original `token`; the audit payload stores only its
digest marker. Results and mutable metadata are not automatically redacted, so
handlers must not copy secrets into their return values.

```powershell
pollard gc runs.db drop-pruned
pollard gc runs.db compact
pollard export runs.db <root-id> subtree.json
pollard import subtree.json archive.db
```

See [Data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md)
for the field-level storage model, retention behavior, and redaction limits.

## Branch, Rollback, And Shared Prefixes

`run.branch()` creates an alternate child cursor while leaving the parent cursor
unchanged. `run.rollback()` moves a cursor to an ancestor, and `run.prune()`
marks an unwanted tip without deleting history. Identical calls beneath the same
parent compute the same node id, so hybrid and replay modes reuse recorded
prefixes before branches diverge.

EXP-001 now includes a pinned llama.cpp/Qwen local-model run with wall-clock,
whole-GPU NVML energy, token, and declared electricity-rate measurements. See the
[logbook](https://github.com/jemsbhai/pollard/blob/main/LOGBOOK.md) and
[findings](https://github.com/jemsbhai/pollard/blob/main/findings.md) for the
exact scope and results.

## Registry Firewall

With a registry installed, `tool_call` cannot execute an arbitrary caller-supplied function. The runtime resolves the tool name and version against `ActionSpec`, validates arguments against the supported schema subset, records the `spec_digest` and `registry_digest`, then runs the registered handler. Unknown tools, version mismatch, invalid args, policy denial, and missing confirmation all produce refusal nodes.

This is structural gating, not content judgment. A content firewall tries to decide whether a requested action is safe. pollard answers a narrower audit question: was this action in the declared, versioned set, with arguments that match its schema, under the recorded policy state?

Dry-run mode records side-effectful registered actions without executing their handlers. This is useful for reviewing an intended action transcript before allowing writes.

How it compares:

- LangGraph and related graph runtimes execute a graph you author ahead of time. pollard ledgers the control flow your code performs and can wrap calls inside a graph node.
- pydantic-ai, smolagents, and the OpenAI Agents SDK own more of the agent loop. pollard is bring-your-own-client and has zero core runtime dependencies.
- Action firewall products judge tool calls by content policy. pollard uses structural registry gating: an action resolves against a versioned registry or it does not execute.
- HTTP recorders pin transport bytes. pollard pins semantic steps, so recordings can outlive SDK or provider changes.

## Record, Replay, And Revalidate

`Runtime(mode=...)` accepts three modes:

- `record`: execute the function and store the result. For compatibility, the
  default redispatches an existing identity and retains result-conflict
  metadata, as in Pollard 1.5.
- `hybrid`: serve a stored result when the computed node id already exists, otherwise execute and store.
- `replay`: never call a step function, registered handler, or live policy hook.
  A missing run, structural node, or result raises `MissingRecording`.

Replay mode verifies stored ancestry before returning any node and does not
create or patch recording state. A SQLite path passed directly to `Runtime` is
opened in query-only mode. When `hybrid` or `replay` serves a stored result,
`run.report()["avoided"]` records the charges that were skipped for that run.
Those pure-replay counters remain process-local.

For cost-sensitive live calls, set
`Runtime(..., refuse_duplicate_recordings=True)`. A sequential duplicate then
raises `DuplicateRecording` before budget reservation, registered-tool policy
evaluation, or dispatch and adds neither spent nor avoided charges. Supply a
new caller-managed `attempt` for a distinct retry, or use `hybrid` to reuse the
stored result. The guard is an existence check rather than an atomic
exactly-once claim; concurrent workers still require provider idempotency.

Live provider comparison is a separate, explicit operation. In `record` mode,
`run.revalidate_model_call(...)` verifies the golden recording before dispatch,
stores the live result under a new budgeted observation node, and writes
value-free comparison evidence without changing the golden node. See
[Replay and live revalidation](https://github.com/jemsbhai/pollard/blob/main/docs/revalidation.md)
for execution fingerprints, comparators, async use, and interpretation limits.

For pytest, install pollard with the `dev` extra or with pytest available, then use the fixture:

```python
def test_agent(pollard_run):
    node = pollard_run.model_call(payload, fn=real_client)
    assert "invoice" in node.result["text"].lower()
```

Run with `--pollard-mode=record`, `--pollard-mode=hybrid`, or `--pollard-mode=replay`. The fixture stores small SQLite recordings under `tests/pollard_recordings/` by default.

## Export Seals

`seal(store, root_id)` returns a rolling SHA-256 report over a subtree's node ids
and result digests. The final digest can be stored beside an exported run:

```python
from pollard import Runtime, seal

rt = Runtime()
with rt.run("audit") as run:
    run.note({"status": "ready"})
    report = seal(run.store, run.root_id)

print(report.digest)
print(report.to_dict())
```

The seal validates each visited node before hashing it. Mutable metadata is not
included; see [Export seals](https://github.com/jemsbhai/pollard/blob/main/docs/seal.md)
for the field-level design.

`SQLiteSealSink` is a reference external custody log. It appends sequence,
store ID, root ID, seal algorithm, digest, UTC time, and signer identity to a
SQLite file kept outside the Pollard database. The deployment remains
responsible for separate access control, signatures, keys, and immutable
retention.

## Store Backends

Core pollard includes `MemoryStore` and `SQLiteStore`. Transactional remote
stores are available through `pollard[pg]`, `pollard[redis]`,
`pollard[mongodb]`, and `pollard[neo4j]`. `pollard[kafka]` supplies an ordered
non-arbiter event store. See
[Distributed store operations](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
before selecting a remote backend. The optional hashrope backend keeps an
append-only operation log inside a hashrope rope:

```powershell
pip install "pollard[hashrope]"
```

```python
from pollard import HashRopeStore, Runtime

store = HashRopeStore()
with Runtime(store).run("hashrope-demo") as run:
    run.note({"checkpoint": "stored in a hashrope log"})

snapshot = store.to_bytes()
reopened = HashRopeStore(snapshot)
assert reopened.get(run.root_id).payload == {"run": "hashrope-demo"}
```

See the [offline examples](https://github.com/jemsbhai/pollard/tree/main/examples)
for scripts that run without network access.

## Tokenmaster Governance

The optional tokenmaster integration records model-call usage, can enforce a
registered model's per-request limits before dispatch, and can account for
tiered request prices:

```powershell
pip install "pollard[tokenmaster,estimate-openai]"
```

```python
from pollard import Budget, Runtime
from pollard.estimators.openai import OpenAITokenEstimator
from pollard.meters import tokenmaster_governance_meters

rt = Runtime(
    meters=tokenmaster_governance_meters(
        model="openai:gpt-5.6-sol",
        estimator=OpenAITokenEstimator(model="gpt-5.6"),
        reserved_output=1_024,
        expected_remaining_turns=5,
        enforce_profile_limits=True,
    )
)

with rt.run(
    "tokenmaster-demo",
    budget=Budget(tokens=120_000, usd="5.00", steps=20),
) as run:
    node = run.model_call(
        {
            "model": "gpt-5.6",
            "input": "Summarize the incident.",
            "max_output_tokens": 1_024,
        },
        fn=lambda _payload: {"usage": {"input_tokens": 1000, "output_tokens": 300}},
    )
    print(node.meta["charges"]["tokens"])
    print(node.meta["charges"]["usd"])
    print(node.meta["tokenmaster"]["state"]["zone"])
```

Use `TokenmasterMeter` instead of the built-in `TokenMeter` when you want
tokenmaster state and recommendations in the audit record. Its optional
estimator plus `reserved_output` enables a conservative precheck before model
dispatch. `enforce_profile_limits=True` also checks the selected tokenmaster
profile's input, context, and explicit output limits for each request and
requires an estimator. These profile limits are not a `Budget(tokens=...)`:
the budget remains cumulative across calls. The settled token charge includes
cache and reasoning fields when present. If settled usage crosses a profile
limit, Pollard records diagnostics on the completed node; it does not rewrite
history or pretend that provider work was avoided.

If the installed `tiktoken` release does not yet recognize a GPT-5-family model
or its `openai:` alias, `OpenAITokenEstimator` uses the GPT-5-family
`o200k_base` fallback. The result remains an estimate, not a provider token
count.

`TokenmasterCostMeter` selects the tokenmaster pricing tier for the request,
uses a conservative preflight estimate, and settles from provider usage. It
records an audit charge whenever configured, but only an explicit
`Budget(usd=...)` turns that charge into a run limit. This is an
application-side governance limit, not a provider-account billing cap, and the
provider's invoice is authoritative.

Tokenmaster governance is deliberately opt-in. Passing
`tokenmaster_governance_meters(...)` is the shortest safe configuration path:
it retains Pollard's standard step, depth, and wall-clock accounting, replaces
the default token meter, and adds USD accounting. Existing `Runtime()` and
`AsyncRuntime()` defaults are unchanged. Applications that need a custom meter
set may continue to construct `TokenmasterMeter` and `TokenmasterCostMeter`
directly.
When `provider_usage` is available, settlement derives exclusive
ordinary-input, cache-read, cache-write, output, and reasoning categories from
it; otherwise it uses normalized exclusive usage. It never adds an inclusive
aggregate a second time.

For direct OpenAI calls, an omitted meter model may be inferred from the
request and completed provider result. Bind `model="openai:..."` explicitly
when the request's `model` is an Azure deployment name, gateway route, or other
alias whose underlying model Pollard cannot prove. An explicit binding is not
replaced by a provider-returned model name. Pricing that cannot be derived from
per-request usage fails closed; in particular, Gemini token-hour cache-storage
charges require separate caller-side accounting and are not approximated by
`TokenmasterCostMeter`.

Pollard and tokenmaster registry lookup are offline at import and runtime.
Neither package fetches provider pages or silently changes a model profile or
price while an application is running. A tokenmaster maintainer explicitly runs
`tokenmaster-models check`, `propose`, `discover`, or `apply`, reviews the
result, and releases the updated registry; its weekly workflow only reports
drift and uploads evidence. Pollard consumes the reviewed registry version
installed by the application. See tokenmaster's
[registry refresh contract](https://github.com/jemsbhai/tokenmaster/blob/main/docs/registry-refresh.md).

The complete classified inventory of package limits is in
[Documented limitations](https://github.com/jemsbhai/pollard/blob/main/docs/limitations.md).

## End-to-End Case Studies

EXP-006 records three complete governed workloads: research over pinned local
documents, a code fix against a pinned repository and test suite, and a
household order across three local MCP stdio servers. Each recording uses the
OpenAI-compatible adapter with a pinned local model, registry-firewalled tools,
a rejected branch, rollback, a selected branch, CLI verification, a subtree
seal, and a content-free HTML tree. No hosted model was called; EXP-006 provider
spend was 0 USD.

The committed evidence contains 49 nodes and six root-to-leaf paths. Verify all
input and artifact hashes, nodes, seals, registry digests, and paths without a
model, MCP SDK, optional dependency, credential, or network connection:

```powershell
$env:PYTHONPATH = (Resolve-Path src)
python examples\exp_006_verify.py
```

Strict replay uses sentinel functions that fail if Pollard attempts to execute
a model or tool handler. The expected result reports zero executed model calls,
zero executed tool calls, and no network use. See the
[EXP-006 case-study index](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/README.md)
for the manifest, seals, HTML trees, recording prerequisites, and claim limits.

The research synthesis is model-generated. The code-fix and household cases
use deterministic candidate controllers with model review; they are workflow
and governance evidence, not evidence that the model autonomously invented the
candidate patches or orders.

## Evidence

Every number below is scoped to its committed protocol and raw artifact. It is
not a hosted-provider, throughput, availability, or total-cost claim.

| Experiment | Recorded result |
|---|---|
| EXP-001 | Shared-prefix local inference reduced mean wall-clock by 40.05%, 59.13%, and 68.54% at 2, 4, and 8 branches; the corresponding whole-GPU NVML energy reductions were 35.23%, 58.53%, and 67.58%. |
| EXP-004 | At 200 synthetic turns, the plain SQLite file was 38.93 times the interned file; fitted finite-range log-log exponents were 1.970694 and 1.201388. |
| EXP-005 | Across 1,650 PostgreSQL contention rounds, exact limits never exceeded the configured limit; maximum estimator overshoot was 6 charges and never exceeded its registered bound. |

The [evidence index](https://github.com/jemsbhai/pollard/blob/main/evidence/README.md)
links protocols, raw JSON, reproduction commands, and limitations. The
[experiment logbook](https://github.com/jemsbhai/pollard/blob/main/LOGBOOK.md)
and [findings index](https://github.com/jemsbhai/pollard/blob/main/findings.md)
retain the interpretation and claim history.

## 1.0 Stability Covenant

Starting with 1.0.0, these interoperability surfaces are frozen until 2.0:

- the `pollard/v1` node-identity domain and identity document;
- canonical identity serialization, including its supported value types;
- the public `Store` protocol method signatures and meanings; and
- the synchronous and asynchronous step-function result contracts.

Other public APIs follow Semantic Versioning. The covenant, exact byte rules,
and advance deprecation policy are specified in the
[API stability policy](https://github.com/jemsbhai/pollard/blob/main/docs/api-stability.md).
Version 1.0.0 activates this covenant. Incompatible changes to a frozen surface
require 2.0.

## Documentation And Release Policy

The [documentation index](https://github.com/jemsbhai/pollard/blob/main/docs/README.md)
links the complete operator guides for providers, data governance,
observability, scale-out stores, seals, examples, evidence, the
[public API reference](https://github.com/jemsbhai/pollard/blob/main/docs/api-reference.md),
and API stability.
All repository README links are absolute HTTPS URLs so the same content renders
correctly on PyPI.

GitHub Actions validates Pollard but never publishes it. A maintainer builds one
artifact set from the reviewed `main` commit, creates the tag and GitHub release
locally, and uploads those same files directly to production PyPI with a local,
project-scoped token. The complete checkpoints and failure recovery are in the
[local-only release runbook](https://github.com/jemsbhai/pollard/blob/main/docs/releasing.md).
