# Troubleshooting

Start with the content-free CLI. It exposes topology, charges, refusals, and
integrity without printing prompts, tool arguments, or results:

```powershell
pollard runs runs.db --json
pollard show runs.db <root-id>
pollard report runs.db <root-id> --json
pollard verify runs.db <root-id> --json
```

Use `--payloads` only in an approved destination. Pollard recordings can
contain the full model request and result even though default CLI output hides
them.

## BudgetExceeded

`BudgetExceeded` means a precheck refused a model or tool step and recorded a
refusal node. The exception's `refusal_id` identifies it. Inspect that node and
the charge report. Common causes are:

- an exact step, depth, or request-window limit has no capacity remaining;
- an input-token estimate plus reserved output exceeds the active token budget;
- another process holds a shared transactional reservation;
- a resumed run reuses the same root-scoped budget or request window; or
- the runtime and application disagree about which logical store is shared.

The refused callable did not run. This guarantee does not apply to a completed
provider call whose actual usage settles above an estimate; that external spend
has already occurred, and later steps are refused.

For transactional stores, inspect renewal errors and database interruptions.
Calls renew their reservation while running. Do not manually edit arbiter
tables.

`ReservationUncertain` means a reserve or release transaction could not be
confirmed after reconnect. `SettlementUncertain` means the provider callable
completed but its database settlement could not be confirmed. Do not repeat
the provider call. Use the reservation ID and the recovery procedure in
[PostgreSQL operations](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md).

A `call_outcome_unknown` note means a direct adapter or generic callable
reported that dispatch occurred but the external result was not known. A
`call_recording_failed` note means the callable returned, but local meter or
result processing failed before a replayable result could be stored. Both notes
contain a blocked payload digest and error type, not the prompt, result, provider
message, or raw response. Treat either note as consumed external capacity and do
not retry automatically.

## PolicyViolation or ConfirmationRequired

For a registry refusal, confirm all of these values:

- tool name and version match one `ActionSpec` exactly;
- the action schema uses Pollard's supported JSON Schema subset;
- required fields are present and no forbidden extra field is supplied;
- policy state permits the call;
- side-effectful actions have the required confirmation; and
- replay uses the same registry digest and redacted identity payload.

`ConfirmationRequired` carries a resume token. Treat it as a capability: do not
log it in an untrusted destination. A dry run records the intended action but
does not execute a side-effectful handler.

## MissingRecording

Replay never falls back to live execution. `MissingRecording` means the
computed run root, structural node, or result-bearing node was not present.
Compare the recorded and current:

- parent node ID;
- node kind and attempt number;
- complete payload, including model, prompt, tools, provider metadata, and
  reserved `_pollard` fields; and
- registry name, version, schema, and redaction marker where applicable.

Strict replay does not evaluate current policy hooks because it cannot execute
the action. Record and hybrid modes apply current policies before execution or
reuse. When a SQLite path is passed directly to a replay runtime, Pollard opens
it query-only; a legacy schema must be migrated deliberately through a writable
copy before it can be replayed read-only.

Use hybrid mode only during deliberate recording. CI should use replay mode and
must not receive provider credentials.

## Live revalidation failure or mismatch

Live revalidation is deliberately separate from strict replay. Call it from a
`record` runtime while the cursor is positioned at the golden model call, and
supply the exact recorded payload and attempt number. Pollard verifies the
golden node before dispatch. A missing or changed golden recording raises
`MissingRecording` without calling the provider.

A completed comparison does not raise merely because results differ. Inspect
`report.matched` and the value-free evidence in `report.comparison`. The
built-in normalized comparator ignores documented volatile response IDs and
usage fields and normalizes tool-call IDs and JSON arguments; use the exact
comparator when byte-for-byte result equality is the contract.

The live observation is a separately metered provider call. Provider failures
propagate, and any dispatched work must be accounted for like an ordinary live
call. A comparator failure records a content-free failure note and then
propagates the error. Reusing an existing observation identity is refused to
prevent accidental sequential duplicate dispatch, but this is not distributed
idempotency across independent writers.

## IntegrityError or verify findings

Stop using the recording as evidence. Do not repair node IDs or result digests
by editing the database. Preserve a read-only copy, capture `pollard verify
--json`, and compare against an independently stored seal or source artifact.

An identity finding means the node's stored parent, kind, attempt, or payload no
longer hashes to its ID. A result finding means result text no longer hashes to
its digest. Missing parents, traversal anomalies, or an export seal mismatch can
also indicate an incomplete transfer.

Restore from a trusted backup or re-record under a new evidence artifact. A new
seal over changed data does not prove that the old recording was valid.

## Provider authentication, model, and quota errors

Pollard passes provider exceptions through because the caller owns the SDK.
Before retrying, check:

- the correct environment, profile, workload identity, or token provider is
  active;
- the credential is scoped to the intended account, project, resource, or
  workspace;
- endpoint, Region, model ID, deployment name, and API version agree;
- the model is enabled and available in that Region or project;
- the principal has inference and any separate token-count permission;
- provider quotas, rate limits, spending limits, and remaining credit permit
  the call; and
- SDK, framework, proxy, and gateway retries are disabled or explicitly
  budgeted.

Do not paste a credential into a model payload to test it. Use the provider's
credential diagnostic outside Pollard, then rerun with the same capped prompt.

## Unexpected duplicate provider calls

Check every layer that can retry or issue an internal request:

- provider SDK retries;
- HTTP transport retries;
- LiteLLM or gateway retries;
- agent framework model or validation retries;
- a token-count request used during precheck;
- a hybrid cache miss caused by changed identity; and
- an application loop that submits the same logical work under a different
  parent or attempt number.

Pollard never retries a step function on its own. A node is stored after a
successful function result or completed stream. Provider errors are not turned
into successful cached results.

## SQLite locked or PostgreSQL unavailable

SQLite serializes writers and is intended for one host. Keep transactions
short, avoid network filesystems, and use PostgreSQL for several hosts or
sustained contention. Do not run `pollard gc` while another process writes the
same store.

For PostgreSQL, verify DSN reachability, TLS, database and schema permissions,
first-use create privileges, sequence use, and matching `store_id`. The
preferred CLI form is `pg-env:VARIABLE#store-id` so a password does not appear
in process arguments.

After a server restart or backend termination, create a new `PostgresStore` or
call `reconnect()` on the existing instance. A schema migration requirement or
unknown schema version is intentional refusal, not a connectivity error.

The same `reconnect()` rule applies to Redis, MongoDB, Neo4j, and Kafka. Redis
requires persistent no-eviction storage. MongoDB refuses a standalone server
because it cannot run the required transactions. Neo4j writes and reads are
routed to a primary. Kafka refuses multiple partitions, compaction, finite
retention, a truncated log, malformed events, or a changed store key. See the
[distributed store runbook](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
for exact configuration checks and recovery steps.

For MongoDB CLI inspection, merge sources, and merge destinations, use
`mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id`. Direct
`mongodb://` and `mongodb+srv://` arguments are rejected to keep credentials out
of supported process arguments. Confirm that the URI variable is set and that
the database, collection prefix, and store id match the writer exactly.
Destinations must explicitly provide all three and are existing-only by
default. Missing schema is an intentional refusal, not an empty store.

For Neo4j CLI inspection, merge sources, and merge destinations, use
`neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`.
Both auth references are required, while database and store id default to
`neo4j` and `default` for sources. Destinations must explicitly provide all
three environment references, database, and store id. CLI labels intentionally
omit both auth references. Confirm every variable, the selected database, the
logical store id, and primary routing. Direct Neo4j and Bolt URI arguments are
rejected. Missing schema, coordinator, exact constraint metadata, or an owned
online range index is an
intentional existing-only refusal, not an empty logical store.

For Kafka CLI inspection, merge sources, and merge destinations, use
`kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id`. Topic is required,
timeout defaults to 30 seconds, and store id defaults to `default` for sources.
A destination requires the explicit configuration reference, topic, and
nonempty store-id fragment. Confirm that the environment variable contains a
JSON object with unique keys, a nonempty string `bootstrap.servers`, and only
string, boolean, integer, or finite-number values. Direct `kafka://` broker
arguments are rejected. A timeout during open can mean that complete replay
from offset zero needs a larger positive integer, not that Pollard can safely
skip older events.

The Kafka CLI label intentionally omits timeout and every client-configuration
value. `plugin.library.paths`, nested configuration, and non-scalar callbacks
are not supported on this path. Use the Python API when the deployment requires
callback-based authentication or another configuration object.

## Remote Store Refusal Or Uncertain Outcome

Treat schema, topology, and topic-configuration errors as intentional
fail-closed checks:

- Redis needs an intact store identity and revision. Confirm persistence,
  `maxmemory-policy noeviction`, the URL, prefix, and `store_id`. A Redis source
  uses `create=False`, so a missing namespace is an error. A merge destination
  must use `redis-env:` with an explicit prefix and store id and also uses
  `create=False` by default. Pass `--initialize-if-missing` only when a missing
  `redis-env:` destination should be created. Review both selectors first
  because a misspelling can create a different namespace when that flag is
  present. Direct `redis://` and `rediss://` forms are legacy source-only
  selectors and are rejected as destinations.
- MongoDB needs a replica set or sharded deployment plus an exact schema,
  coordinator, and compatible unique record index at the selected database,
  collection prefix, and `store_id`. Missing, partial, or ambiguous state is an
  integrity refusal; do not add records or indexes manually to make it pass.
  Inspect the exact two collections and index metadata, recover from a known
  backup, or remove only a proven-empty mistyped namespace. A destination uses
  `create=False` unless `--initialize-if-missing` is supplied. Fresh
  initialization creates index DDL separately from its atomic
  schema/coordinator transaction, so a later failure can leave an empty
  index-only shell that should be inspected before exact retry. Do not use
  `directConnection=true` as a production substitute for normal topology
  discovery. Stabilize writers for verification, seal, export, or merge because
  traversal spans multiple snapshot transactions and merge application is
  node-by-node.
- Neo4j needs write routing and access to the selected database. Existing-only
  sources and destinations need read privilege on `_PollardKV` and
  `_PollardCoordinator` nodes plus permission to inspect the exact two named
  uniqueness constraints. They issue no writes or constraint DDL. A destination
  also needs Pollard node-write privileges. Pass `--initialize-if-missing` only
  for a reviewed, explicit `neo4j-env:` destination that should create a
  completely fresh logical namespace. That path needs constraint-create
  privilege unless an administrator already installed the exact shared
  constraints. Constraint DDL is database-wide and outside the atomic
  schema/coordinator transaction; never delete shared constraints as automatic
  cleanup. Schema-only, coordinator-only, data-only, wrong-identity, and
  incompatible or offline constraint/index states are integrity refusals and
  are not repaired.
  Quiesce writes when verification, sealing, export, or transfer must represent
  one evidence boundary because a command can span multiple read-committed
  transactions.
- Kafka needs one pre-created topic, exactly one partition, delete-only cleanup,
  infinite time and byte retention, and history beginning at offset zero. Its
  observational role needs topic metadata, DESCRIBE_CONFIGS, READ, and, where
  coordinator discovery is authorized separately, GROUP DESCRIBE on the
  deterministic `pollard-observer-<sha256(topic + NUL + store_id)>` group. It
  does not need a producer, topic WRITE, or IDEMPOTENT_WRITE. A completely
  pristine cluster can initialize broker-owned `__consumer_offsets`
  infrastructure in response to librdkafka's `FindCoordinator`; pre-provision
  that state when even broker-internal first use is prohibited.
  A merge destination additionally needs topic WRITE, the cluster permission
  required for an idempotent producer, and narrowly scoped GROUP DESCRIBE for
  `pollard-reader-<sha256(topic + NUL + store_id)>`. It needs no CREATE, ALTER,
  or DELETE privilege because Pollard never provisions or removes topics.
  Destinations must already be populated: replay must materialize at least one
  node proving the explicit store id. Missing, empty, wrong-identity, corrupt,
  truncated, or incompatible topics fail before producer construction and
  publish nothing. `--initialize-if-missing` is invalid for Kafka; provision
  the topic administratively and seed the first node with a reviewed Python
  writer.

A Kafka read-only source replays `[0, high)` once and freezes that view.
Concurrent appends become visible only after `reconnect()`. If a verification,
seal, or export must represent a drained final topic rather than a coherent
historical prefix, stop writers and independently record the topic, partition
zero, and exclusive high watermark. A wrong store id fails when a retained
event is decoded, but an empty topic has no event that can prove which store id
was intended. If reconnect reports that history changed before the prior replay
boundary, treat topic truncation, deletion/recreation, or same-offset rewriting
as an integrity incident; the prior clients and frozen view remain installed.

`ReservationUncertain` and `SettlementUncertain` mean the server may have
committed even though the client could not confirm it. Stop new dispatch for
that logical store and reconcile the same reservation id and exact request or
charges. Do not create a replacement id or release capacity based only on a
transport error. Account for an ambiguous dispatched call at its reserved
ceiling until reconciliation succeeds.

`ReservationLeaseLost` means a completed call outlived its last confirmed
lease. The call and available accounting evidence remain recorded, but the
shared precheck guarantee no longer covers that interval. Investigate database
latency, failover, worker scheduling, and lease duration before resuming.

## Import or merge failure

Import verifies the full subtree before writing. Confirm the JSON is complete,
the seal report matches, and any external parent already exists in the target.

Merge rejects unequal identity fields under the same node ID. In default mode,
result or metadata conflicts are retained in destination metadata; in
`--replay` mode, a result conflict is an integrity error. Repeating a successful
merge is idempotent. The CLI may reject an invalid destination selector or flag
combination first, but that check does not read a referenced destination
environment variable or construct a client. It then opens every source in
command-line order, traverses and validates it fully, serializes its exact nodes
to a uniquely named SQLite spool in a private temporary directory, closes the
source, and validates the finalized spool. All source spools must finish before
the CLI resolves destination configuration or constructs a Redis, MongoDB,
Neo4j, or Kafka destination, so source open, traversal, validation,
serialization, disk-write, finalization, corruption, and truncation failures
leave the destination untouched. Missing remote destinations fail before
writes by default. With `--initialize-if-missing`, Redis initializes
identity, schema, and revision atomically; MongoDB initializes schema and
coordinator revision atomically after separate physical index setup; Neo4j
initializes schema and coordinator atomically after separate global constraint
setup. The flag is invalid for Kafka. A Kafka destination instead proves a
populated existing identity through full replay and constructs its producer
only after topic, configuration, history, and prefix validation. Copying nodes
and metadata is still not one cross-node or cross-backend transaction.
If copying fails, accepted changes can remain; quiesce destination writers,
inspect the destination, rerun the exact merge, verify, and seal. Concurrent
writers can race merge metadata updates. Kafka destination events accepted
before failure are irreversible through Pollard; an exact rerun publishes no
new events. Preparation uses bounded working memory but temporary disk
proportional to all sources. Spools contain payloads, results, and metadata, so
check the operating system's temporary-directory permissions, free space, quota,
antivirus or backup interference, and filesystem health after a preparation
error. Pollard attempts to remove the private spool directory on every exit,
including source, constructor, and destination-body failures. A cleanup failure
can leave sensitive private artifacts and returns exit code `2`. On its own it
uses a fixed credential-safe error. When another failure already exists, that
primary source or destination attribution remains and a fixed cleanup notice is
added. If cleanup failure follows destination application, accepted writes
remain; inspect the destination and rerun the exact merge after manually
securing and removing any residual private preparation directory. A Kafka merge
source supplies the materialized node tree,
not the original Kafka offsets, operation ids, or command history. `import` and
`gc` remain SQLite-only.

## CLI exit codes

- `0`: command completed, or verification found no integrity problem.
- `1`: verification completed and reported one or more findings.
- `2`: invalid command, unreadable input, missing node, optional dependency
  error, or another handled Pollard or operating-system error.

In automation, consume `--json` output and the exit code. Human-readable tree
formatting is not a stable machine interface.

## Diagnostic bundle

When opening an issue, include:

- Pollard and Python versions;
- operating system and store backend;
- redacted install and invocation commands;
- provider and model or deployment name, if relevant;
- content-free `runs`, `show`, `report --json`, and `verify --json` output;
- the exception type and message; and
- the smallest offline reproducer or frozen provider response fixture.

Do not include API keys, tokens, DSNs, prompts, results, customer data, signed
URLs, resume tokens, or a database file unless the issue channel is approved
for that data.
