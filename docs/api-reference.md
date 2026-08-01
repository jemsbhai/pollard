# Public API reference

## Distributed store backends

`RedisStore`, `MongoStore`, and `Neo4jStore` implement `Store`,
`TransactionalArbiter`, and `RenewableArbiter`. `KafkaStore` implements the
frozen `Store` protocol as an append-only event log, but intentionally does not
implement shared arbitration or offline garbage collection. See
[`scale-out.md`](scale-out.md) for the capability and operations boundaries.

This reference covers the supported public Python surface in Pollard 1.x. Names
listed in `pollard.__all__` can be imported from the package root. Provider
adapters, meters, estimators, MCP helpers, and OpenTelemetry helpers live in
their documented submodules.

The [API stability policy](https://github.com/jemsbhai/pollard/blob/main/docs/api-stability.md)
defines the four surfaces frozen for all 1.x releases. Other public names follow
Semantic Versioning and the documented deprecation period.

## Minimal runtime

```python
from pollard import Budget, Runtime

with Runtime("runs.db").run("triage", budget=Budget(tokens=2_000, steps=4)) as run:
    node = run.model_call(
        {"model": "mock", "input": "hello"},
        fn=lambda _payload: {
            "text": "hello",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    print(node.id, node.result["text"], run.report())
```

`Runtime()` with no argument uses `MemoryStore`. A string or `Path` creates a
`SQLiteStore`. Passing a `Store` instance uses that caller-owned backend.

## Runtime and AsyncRuntime

```python
Runtime(
    store=None,
    *,
    meters=None,
    registry=None,
    policies=None,
    dry_run=False,
    mode="record",
    on_node=None,
    reservation_lease_seconds=60,
)
```

- `store`: `None`, SQLite path, or a `Store` implementation.
- `meters`: ordered meter list. The default is `StepMeter`, `DepthMeter`,
  `WallClockMeter`, and `TokenMeter`.
- `registry`: frozen `Registry` used to resolve versioned tool calls.
- `policies`: ordered policy objects. Each returns `ALLOW`, `DENY`, or
  `CONFIRM`.
- `dry_run`: when true, registered actions marked `side_effects=True` are
  recorded without executing their handlers.
- `mode`: `record`, `hybrid`, or `replay`, as a string or `ReplayMode`.
  Path-based SQLite stores are opened query-only in replay mode.
- `on_node`: optional callback invoked after a new node is safely stored. A
  callback error becomes a warning and does not discard the node.
- `reservation_lease_seconds`: positive lease used by transactional stores for
  shared budget and window reservations.

`Runtime.run(label, budget=None, attempt=0)` creates or opens the deterministic
run root and returns a cursor at that root. `Runtime.resume(...)` requires that
root to exist and places the cursor at its deepest non-pruned leaf.

`AsyncRuntime` accepts the same constructor and run arguments. Its run context
returns `AsyncRun`; store operations remain synchronous while model and tool
step functions may be async or async-streaming.

## Run cursor

Every step is a child of `run.cursor_id`. A successful call advances that
cursor. A refusal also records a child and advances the cursor before raising.

### model_call

```python
run.model_call(
    payload,
    *,
    fn,
    attempt=0,
    on_delta=None,
    keep_chunks=False,
) -> Node
```

The payload must use Pollard's canonical identity value types: null, string,
boolean, integer, lists, and string-keyed objects. Floats and bytes are rejected
for identity data. `fn(payload)` returns a result dictionary or an iterator of
chunk dictionaries.

`on_delta` receives stream chunks in order. `keep_chunks=True` stores the raw
ordered chunks under `result["chunks"]`; replay re-emits them. Charges settle
once after complete stream consumption.

### revalidate_model_call

```python
run.revalidate_model_call(
    payload,
    *,
    fn,
    contract,
    comparator=None,
    live_payload=None,
    attempt=0,
    observation_id=None,
    on_delta=None,
    keep_chunks=False,
) -> RevalidationReport
```

`revalidate_model_call` is an explicitly live operation available only in
`record` mode. Before dispatch it derives and integrity-verifies the golden
model-call node from the current parent, payload, and attempt. A missing
recording, invalid ancestry, reused observation ID, non-record mode, or dry run
fails before `fn` executes.

The new provider result is stored under a unique sibling model-call node and is
metered, reserved, and settled like any other live call. A value-free immutable
note below that observation records result digests, comparator status, and JSON
Pointer difference paths. The golden node is not changed, and after success the
cursor advances to it. `AsyncRun.arevalidate_model_call` provides async and
async-streaming parity.

The generated observation ID is unique by default. The existing-node check for
a caller-supplied ID prevents sequential reuse but is not a distributed
dispatch lock; workers must not coordinate provider idempotency through this
field.

By default the live callable receives the recorded payload. `live_payload`
allows deliberate comparison against a different model or request while the
first `payload` continues to identify the golden node. If an explicit live
payload is bound to a `ReplayContract`, that fingerprint must equal `contract`
or Pollard refuses before dispatch.

`ReplayContract(provider, ...)` is a caller-declared execution fingerprint.
Optional fields are `model_revision`, `api_version`, `adapter`,
`adapter_version`, `sdk`, `sdk_version`, `application_revision`, and an
identity-safe `environment` object. `ReplayContract.bind(payload)` returns a
copy containing the fingerprint under reserved `_pollard.replay_contract`
metadata. It is a declaration, not provider attestation.

The default `NormalizedModelComparator` compares normalized text, tool calls,
refusals, or structured output while excluding volatile provider identity and
accounting fields. `ExactResultComparator` compares the complete result.
Caller-defined `RevalidationComparator` implementations return a
`RevalidationComparison`; comparator names should be versioned and difference
paths must not contain values.

`RevalidationReport` exposes `observation_id`, recorded/live/evidence node IDs,
comparator name, `matched`, `exact_match`, result digests, difference paths,
recorded and live contracts, live charges, and `to_dict()`. See
[`revalidation.md`](revalidation.md) for the storage layout, examples, and
interpretation limits.

### tool_call

```python
run.tool_call(name, args, *, fn=None, version=None, attempt=0) -> Node
```

Without a registry, `fn` is required and receives the tool-call identity
payload. With a registry, Pollard ignores caller-supplied execution functions,
resolves `name` and `version`, validates and redacts arguments, evaluates
policies, and invokes the registered handler. Unknown, mismatched, invalid, or
denied actions record a refusal and raise `PolicyViolation`.

### notes, branches, rollback, and prune

```python
run.note(payload, *, attempt=0) -> Node
run.branch(*, attempt=0, budget=None) -> RunBranch
run.rollback(node_id=None, *, steps=1) -> Node
run.prune() -> None
run.report() -> dict[str, dict[str, float]]
```

`note` records identity data without running a callable. A branch context starts
at a new branch-anchor note and can add a nested budget scope. Leaving the
context does not move the parent cursor. `rollback` can move only to an ancestor
of the current cursor. `prune` sets mutable metadata and does not delete nodes.
`report` returns settled `spent` charges and run-local replay `avoided` charges.

### confirmation

When a policy returns `Decision.CONFIRM`, `tool_call` raises
`ConfirmationRequired` with a `resume_token`. If the cursor has not moved,
`run.confirm(token)` executes the prepared registered call. Tokens are held in
the current process; they are not durable workflow state.

## Step result contract

A non-streaming step returns a dictionary. Provider adapters normally include:

```python
{
    "text": "normalized text when available",
    "tool_calls": [],
    "usage": {"input_tokens": 10, "output_tokens": 4},
}
```

Adapters retain provider-native fields as well as normalized fields. The
default `TokenMeter` charges only integer `input_tokens` and `output_tokens`
under a result `usage` object.

For a stream, every chunk must be a dictionary:

- `{"result": mapping}` replaces the accumulated result.
- `{"delta": mapping}` recursively merges that mapping.
- Any other chunk merges itself.
- Nested mappings merge, strings concatenate, lists append, and other values
  replace.

These sync and async contracts are part of the frozen 1.0 covenant.

## Budgets and meters

```python
Budget(
    usd=None,
    tokens=None,
    depth=None,
    seconds=None,
    steps=None,
    extra=None,
)
```

Limits are optional. `usd` and `seconds` accept values safely convertible to
`Decimal`; token, depth, and step limits are integers. `extra` maps a custom
meter name to its limit.

Built-in meters from `pollard.meters`:

| Meter | Charge | Precheck behavior |
|---|---|---|
| `StepMeter()` | One per model or tool call | Exact one-step estimate |
| `DepthMeter()` | No additive charge; runtime checks next tree depth | Exact structural check |
| `WallClockMeter()` | Completed callable duration | No duration prediction |
| `TokenMeter(estimator=None, reserved_output_tokens=0)` | Normalized input plus output usage | None without estimator; estimated input plus output reservation with estimator |
| `CostMeter(prices)` | Token usage multiplied by caller-supplied per-million prices | No dollar prediction |
| `TokenmasterMeter(...)` | Normalized token usage plus tokenmaster state and advice | Optional prompt estimate and opt-in per-request profile checks |
| `TokenmasterCostMeter(...)` | Tier-aware tokenmaster request price | Conservative request estimate; only `Budget(usd=...)` creates a dollar limit |
| `WindowMeter(name, limit, window_seconds, meter=None)` | Wrapped meter charge in a shared sliding window | Uses wrapped meter estimate |

`CostMeter` price rows require `input_per_1m` and `output_per_1m`. Pricing is
caller data and must be updated when a provider price changes. A settled
dollar charge is not a provider-account hard limit.

Optional meters include `EnergyMeter` in `pollard.meters.energy` and
`TokenmasterMeter` plus `TokenmasterCostMeter` in `pollard.meters`. The OpenAI
prompt estimator is `pollard.estimators.openai.OpenAITokenEstimator`.
When an installed `tiktoken` does not recognize a GPT-5-family identifier,
including an `openai:`-prefixed alias, that estimator uses `o200k_base`;
other recognized modern OpenAI family prefixes do the same, while genuinely
unknown families retain the general `cl100k_base` fallback.

`TokenmasterMeter(model=None, *, meter=None, estimator=None,
reserved_output=0, expected_remaining_turns=None, task=None, policy=None,
enforce_profile_limits=False, profile_capacity="nominal")` accepts the same
estimator protocol as `TokenMeter`. When configured, its token precheck is the
estimated input plus `reserved_output`; actual tokenmaster state and the
settled charge still come from provider usage.

`tokenmaster_governance_meters(model=None, *, estimator, reserved_output=0,
expected_remaining_turns=None, task=None, policy=None,
enforce_profile_limits=False, profile_capacity="nominal", cost_name="usd")`
returns the standard `StepMeter`, `DepthMeter`, and `WallClockMeter` followed by
configured `TokenmasterMeter` and `TokenmasterCostMeter` instances. Pass the
result explicitly as `Runtime(meters=...)` or `AsyncRuntime(meters=...)`.
It does not change either runtime's defaults.

`enforce_profile_limits=True` additionally checks each request against the
selected tokenmaster profile and requires an estimator. Input and context
checks use estimated input and the greater of `reserved_output` and an explicit
request output limit; the profile's output cap is enforced only for an explicit
`max_output_tokens`, `max_completion_tokens`, or `max_tokens` request. These
are per-request checks. They do not set or replace cumulative
`Budget(tokens=...)`. `profile_capacity` selects tokenmaster's nominal or
effective profile capacity. A refusal is recorded before dispatch when a check
can prove the request is too large. Settled overages are retained as diagnostics
on the completed node and do not undo an external call.

`TokenmasterCostMeter(model=None, *, meter=None, estimator,
reserved_output=0, name="usd")` selects tokenmaster 0.2 pricing for the request
profile, including long-context tiers. The estimator is required. Its preflight
estimate uses the highest applicable input-category rate plus reserved output,
so it is intentionally conservative; settlement uses the provider's exclusive
input, cache-read, cache-write, output, and reasoning usage categories. It
participates in USD arbitration under its default `name="usd"` only when the
run has an explicit `Budget(usd=...)`. A custom name uses the matching
`Budget(extra={...})` entry instead. The resulting charge is an application-side
ledger value, not a provider-account limit or invoice. Without the matching
budget, Pollard records the charge but does not enforce a dollar ceiling.

Both profile prechecks and USD prechecks depend on caller-supplied prompt
estimates. Profile enforcement requires an estimator, and
`TokenmasterCostMeter` always requires one. Images, tools, provider-added
instructions, and wire-format changes can make that local estimate differ from
settled usage. USD estimation deliberately favors a conservative upper bound.
Exact settlement derives exclusive ordinary-input, cache-read, cache-write,
output, and reasoning categories from retained `provider_usage` when available,
then falls back to normalized exclusive usage. It avoids double counting an
inclusive aggregate.

The constructor accepts either `model` or `meter`, not both; that explicit
model or supplied meter profile always wins. When both bindings are absent,
settlement resolves a compatible direct-provider profile from `result.model`,
then `usage.model_id`, then the request's `model`. Set `model` explicitly for
Azure deployment names, model-gateway routes, or provider aliases that do not
identify the underlying tokenmaster profile; neither inference nor a
provider-returned model overrides an explicit binding. Missing, non-USD, or
incomplete request pricing fails closed. Gemini token-hour cache-storage pricing
is therefore rejected rather than silently approximated from request token
counts.

Bundled registry lookup is offline, and its data is not automatically replaced
at runtime. Pollard performs no model-catalog or pricing network access at
import, preflight, or settlement. Tokenmaster maintainers explicitly run
`tokenmaster-models check`, `propose`, `discover`, or `apply`; the scheduled
weekly workflow reports drift and uploads a report but does not mutate the
registry, commit, or publish. Applications receive reviewed data by upgrading
their installed tokenmaster release.

## Registry and policies

```python
ActionSpec(name, version, description, schema, side_effects, handler=None)
Registry([spec, ...])
```

An action spec computes `spec_digest` from every field except the handler. A
registry rejects duplicate names and computes `registry_digest` from its sorted
spec digests. A run root binds to one registry digest.

The zero-dependency schema subset accepts `type`, `properties`, `required`,
`enum`, `items`, `additionalProperties`, non-empty `anyOf` arrays,
`minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `minLength`,
`maxLength`, `minItems`, `maxItems`, the non-validation annotations `title`,
`description`, and `default`, and Pollard's `sensitive` marker. Each `anyOf`
branch uses the same supported subset. Types are object, string, integer,
boolean, array, and null.

The four minimum and maximum keywords require `type: "integer"` and integer
bounds. `minLength` and `maxLength` require `type: "string"` and nonnegative
integer counts. `minItems` and `maxItems` require `type: "array"` and
nonnegative integer counts. `number`, `format`, `pattern`, `multipleOf`, and
other JSON Schema validation keywords remain unsupported.

Finite local `$ref` values into `$defs` or legacy `definitions` are expanded
when the spec is constructed. Missing, external, and cyclic references raise
`UnsupportedSchema`, as do unsupported validation keywords or types. An
annotation does not change argument validation, but remains part of the action
spec digest.

`sensitive: true` is valid on string fields. Pollard validates the original
argument, supplies it to policies and the handler, but hashes and stores a
redaction marker. For `anyOf`, redaction follows every matching branch; if an
invalid value matches none, Pollard applies every branch before recording a
refusal. Handler results and metadata are not automatically redacted.

A policy implements:

```python
def decide(self, ctx: PolicyContext) -> Decision: ...
```

`PolicyContext` contains the resolved spec, original arguments, cursor ID, run
label, and current settled counters.

## Replay modes

`ReplayMode.RECORD` always executes a step function and stores its result.
`ReplayMode.HYBRID` reuses an exact existing result or executes on a miss.
`ReplayMode.REPLAY` never executes a step function, registered handler, or live
policy hook and raises `MissingRecording` on a run, structural-node, or result
miss.

Replay validates stored ancestry before returning roots, notes, branch anchors,
or results. It does not create nodes, bind registry metadata, record refusals,
persist avoided charges, or permit pruning. Identity includes the parent, kind,
payload, and attempt, so equivalent payloads beneath different parents are
distinct steps. Registered-tool replay still resolves and validates the frozen
registry identity, including redaction, before lookup; it does not re-evaluate
live policies because no action is executed.

## Stores

| Store | Constructor | Intended scope |
|---|---|---|
| `MemoryStore` | `MemoryStore()` | Tests and one-process ephemeral runs |
| `SQLiteStore` | `SQLiteStore(path, intern_payloads=True, intern_threshold=1024, read_only=False)` | Persistent one-host runs; query-only inspection or replay with `read_only=True` |
| `PostgresStore` | `PostgresStore(conninfo, store_id="default", ...)` | Transactional multi-process and multi-host runs |
| `RedisStore` | `RedisStore(url=None, *, client_factory=None, store_id="default", prefix="pollard", create=True, watch_retries=64)` | Transactional shared runs on one Redis logical store |
| `MongoStore` | `MongoStore(uri, database="pollard", store_id="default", collection_prefix="pollard", create=True, ...)` | Transactional shared runs on a replica set or sharded deployment |
| `Neo4jStore` | `Neo4jStore(uri, auth, database="neo4j", store_id="default", create=True, ...)` | Transactional shared runs routed through a graph primary |
| `KafkaStore` | `KafkaStore(client_config, topic=..., store_id="default", read_only=False, require_existing=False, timeout=30)` | Ordered append-only audit and replay without shared arbitration |
| `HashRopeStore` | `HashRopeStore(data=b"")` | In-process operation log and byte snapshot |

`Store` is the frozen structural protocol with `put`, `get`, `exists`,
`children`, `update_meta`, `walk`, and `roots`. Custom stores must preserve
content-addressed identity, parent existence, deterministic child order, and
the documented method meanings. Values returned by `get` and `walk` must be
detached from mutable stored state.

SQLite and PostgreSQL intern large string payload leaves by default. Interning
is a storage encoding, not redaction or encryption. The remote extras are `pg`,
`redis`, `mongodb`, `kafka`, and `neo4j`; `stores` installs all five drivers.
Hashrope requires the `hashrope` extra.

`PostgresStore.migrate(conninfo)` performs the explicit legacy-to-current
schema migration and returns `(old_version, new_version)`. It requires a drained
reservation table. `store.reconnect()` replaces a broken connection and checks
the schema version before returning.

`RedisStore`, `MongoStore`, and `Neo4jStore` are runtime-checkable
`TransactionalArbiter` and `RenewableArbiter` implementations. `KafkaStore` is
neither. Kafka also omits the private maintenance capability, so `gc()` refuses
it instead of implying that an append-only broker log was physically erased.

Every remote-store constructor establishes a live connection and validates its
backend contract, initializing a schema where applicable, before returning.
Each implements `close()`, `reconnect()`, `__enter__`, and `__exit__`; use a
context manager for bounded work. These adapters are synchronous. Applications
that use an asynchronous runtime must keep blocking store calls off
latency-sensitive event-loop paths.

Remote driver packages are imported lazily. Importing `pollard` does not
require them, while constructing a store without its extra raises an
`ImportError` that names the required installation command.

Backend-specific constructor behavior:

- `RedisStore` creates keys beneath `prefix`, hashes `store_id` into a common
  Redis key tag, and retries at most `watch_retries` optimistic conflicts.
  With `create=True`, an empty logical namespace initializes its identity,
  schema, and revision atomically in one Redis transaction; an existing
  namespace must match its identity and schema.
  Pass `create=False` to require an existing identity, schema, and revision
  without initializing a missing logical namespace.
  URL-backed construction rejects `decode_responses`, `encoding`, and
  `encoding_errors` query options because Pollard owns string decoding.
  Pass either a URL or a zero-argument `client_factory`. The factory must
  return a fresh synchronous redis-py client with decoded string responses;
  Pollard calls it again on reconnect.
- `MongoStore` passes additional keyword arguments to `pymongo.MongoClient`.
  `collection_prefix` defaults to `pollard` and may contain only letters,
  digits, and underscores after an initial letter. With `create=True`, it
  classifies the namespace first. A fresh store gets the unique record index,
  then schema and coordinator identity/revision initialize in one transaction.
  Index DDL is outside that transaction. Existing or partial state is never
  repaired implicitly. Pass `create=False` to validate a replica-set or mongos
  topology and require an exact schema, coordinator, and unique index without
  creating collections, indexes, coordinator state, or schema records.
  Reconnect uses the same non-mutating validation.
- `Neo4jStore` passes additional keyword arguments to
  `GraphDatabase.driver`. `auth` is the driver authentication value and
  `database` defaults to `neo4j`. With `create=True`, it first classifies the
  logical namespace without writing. An existing namespace must have the exact
  schema, coordinator, and both named uniqueness constraints. A completely
  fresh namespace creates or confirms the database-global constraints outside
  the logical transaction, then initializes schema and coordinator together in
  one managed transaction. Pass `create=False` to require the same exact
  existing state without writes or constraint DDL. Partial, ambiguous, and
  incompatible states are not repaired. `reconnect()` always uses this
  non-mutating existing-state validation, regardless of the original
  `create` value.
- `KafkaStore` accepts a confluent-kafka client configuration mapping and a
  positive finite operation `timeout`. It requires an existing dedicated topic.
  It controls topic auto-creation, acknowledgements, idempotence, consumer group
  and offset behavior, isolation, and earliest replay; unrelated TLS, SASL, and
  transport settings pass through. `read_only=True` creates no producer and
  refuses `put()` and `update_meta()`. It also disables client metrics push,
  captures the exclusive high watermark during construction, replays the
  committed prefix `[0, high)`, and serves every read from that frozen in-memory
  view. `reconnect()` atomically captures a new prefix only when every
  previously observed Kafka record digest remains unchanged at its original
  offset; failed replacement or prefix validation leaves the prior clients and
  view installed. Writable construction validates the existing topic,
  configuration, complete history, and retained prefix before constructing the
  producer. `require_existing=True` additionally refuses producer construction
  unless replay materializes at least one node; the CLI uses that mode for
  destinations. KafkaStore still does not create or initialize a topic.

The Redis CLI form is
`redis-env:VARIABLE?prefix=PREFIX#store-id`. Observation and merge sources open
with `RedisStore(create=False)` and require the selected logical namespace to
exist. A merge destination must use `redis-env:` with an explicit prefix and
store id. It also opens with `RedisStore(create=False)` by default, requiring a
matching existing namespace.

For every CLI merge, Pollard first validates only the destination selector's
syntax and whether the requested flags are eligible for that selector. This
stage does not read destination environment values or construct a destination
client. The CLI then opens each source in command-line order, traverses and
validates it completely, serializes every exact `Node` field into a
uniquely-named SQLite spool in a private temporary directory, closes the source,
and finalizes and validates the spool. Spool records carry deterministic
positions plus record and whole-spool digests. All source spools must pass
SQLite integrity, schema, completion, size, ordering, node-id, record-digest,
and aggregate-digest checks before destination environment lookup, client
construction, initialization, or writes.

Preparation retains only a bounded working set in RAM; temporary disk use grows
with the total prepared sources. The spools are internal artifacts rather than
portable exports and contain payload, result, and metadata content. Pollard
creates unique private paths, never selects a user output path, closes database
handles before removal for Windows compatibility, and attempts to remove the
whole private directory on every exit. Spool creation,
serialization, disk-write, finalization, corruption, or truncation errors are
attributed to the source and fail before destination access. A cleanup error can
leave private artifacts and is a command error with a credential-safe message.
If another error already exists, it retains its source or destination
attribution and receives a fixed cleanup-failure notice. If cleanup fails after
destination application, writes may already have succeeded; inspect the
destination and rerun the exact idempotent merge.

For Redis, `--initialize-if-missing` is valid only for an explicit `redis-env:`
destination. With that flag, the CLI finalizes and validates every source spool
before constructing the destination with
`RedisStore(create=True)`. A missing destination atomically initializes
identity, schema, and revision; an existing one must match. Malformed percent
escapes and whitespace in a destination prefix or store id are rejected.
Direct `redis://` and `rediss://` specifications remain legacy source-only
forms and are rejected as destinations. Redis merge copies nodes and metadata
rather than mutable governance state, is not one cross-node or cross-backend
transaction, and is idempotent on an exact rerun. `import` and `gc` remain
SQLite-only.

The MongoDB CLI form is
`mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id`. Sources may use
the `pollard`, `pollard`, and `default` selector defaults. A merge destination
must explicitly provide database, prefix, and store id and uses
`MongoStore(create=False)` by default. Direct `mongodb://` and
`mongodb+srv://` arguments are rejected.

`--initialize-if-missing` may be used with an explicit `mongo-env:`
destination. Only after all source spools are finalized and validated does the
CLI resolve the destination environment value and construct
`MongoStore(create=True)`.
Missing, partial, malformed-coordinator, or incompatibly indexed existing
state fails closed. Physical index setup is separate from the transaction that
initializes schema and coordinator revision, so a failed construction can leave
an empty index-only shell; Pollard does not infer repairs for partial logical
state. Labels, warnings, and driver failures never render the URI. MongoDB
merge is node-by-node and non-atomic, exact reruns are idempotent, and active
governance state is not copied. Quiesce writers, then verify and seal. `import`
and `gc` remain SQLite-only.

The Neo4j CLI form is
`neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`.
Both auth references are required. Sources may use the `neo4j` database and
`default` store-id defaults. A merge destination must explicitly provide the
URI, user, and password environment references, database, and store id and uses
`Neo4jStore(create=False)` by default. Direct Neo4j and Bolt URI arguments are
rejected.

`--initialize-if-missing` may be used with an explicit `neo4j-env:`
destination. Only after all source spools are finalized and validated does the
CLI resolve the environment values and construct `Neo4jStore(create=True)`.
Existing-only
construction validates schema, coordinator, exact constraint metadata, and
owned online range-index metadata without writes. Fresh initialization checks
logical emptiness before creating
or validating the shared global constraints, then initializes schema and
coordinator atomically. Constraint DDL is a separate database-wide operation;
partial logical states and partial, incompatible, or offline constraint/index
states fail closed. Labels,
warnings, and driver failures omit credential values. Neo4j merge is
node-by-node and non-atomic, exact reruns are idempotent, and active governance
state is not copied. Quiesce writers, inspect and rerun after partial failure,
then verify and seal. `import` and `gc` remain SQLite-only.

The Kafka CLI form is
`kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id`. Topic is required,
timeout is a positive integer defaulting to `30`, and store id defaults to
`default` for sources. A merge destination requires the explicit configuration
reference, topic, and nonempty store-id fragment. The referenced JSON object
must have unique keys, a nonempty string `bootstrap.servers`, and only string,
boolean, integer, or finite-number values. The CLI rejects plugins and
suppresses native debug/log output. Its label contains the configuration
reference, topic, and store id, but not timeout or any configuration value.

Observation and merge sources use `KafkaStore(read_only=True)` and freeze one
producer-free prefix. A merge destination is existing-and-populated only: full
replay must materialize at least one node and prove the explicit store id.
Missing, empty, wrong-identity, corrupt, truncated, or incompatibly configured
topics fail before producer construction and publish nothing. Pollard never
creates topics, and `--initialize-if-missing` is invalid for Kafka. An operator
must provision the topic and a reviewed Python writer must seed its first node.
Every source spool is finalized and validated before destination configuration
lookup or access; the producer is constructed only after destination topic,
configuration, history, and prefix validation.

Kafka merge appends deterministic node and metadata commands one at a time
using `acks=all`, idempotence, and replay confirmation. It is non-atomic and
accepted events are irreversible through Pollard, while an exact rerun
publishes no new events. Quiesce writers, inspect and rerun after partial
failure, then verify and seal. Concurrent writers can race merge metadata.
Kafka remains neither an import nor a garbage-collection target and does not
provide shared arbitration or record-level GC.

See the
[distributed-store operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
for environment mappings, a runnable record/replay example, production
requirements, uncertainty handling, migration, and recovery.

## Nodes and reports

`Node` is an immutable dataclass with `id`, `parent`, `kind`, `attempt`,
`payload`, `result`, `result_digest`, and mutable-dictionary `meta` fields.
`Node.make(...)` computes identity and result digests for a new node. Application
code normally receives nodes from a run or store rather than constructing them.

`NodeKind` values are `ROOT`, `MODEL_CALL`, `TOOL_CALL`, `NOTE`, and `REFUSAL`.
The stored `node.kind` value is the corresponding lowercase string.

`VerifyReport` contains `ok` and a list of `VerifyFinding(node_id, message)`.
`SealReport` contains the root, algorithm, final digest, and ordered
`SealEntry` values. `MergeReport`, `ExportReport`, `ImportReport`, and
`GCReport` summarize their named operation and expose `to_dict()` for JSON-safe
reporting. `pollard.__version__` is the installed package version string.

## Integrity, transfer, and retention

```python
verify(store, node_id) -> VerifyReport
seal(store, root_id) -> SealReport
merge(destination, source, replay=False) -> MergeReport
export_subtree(store, root_id, path) -> ExportReport
import_subtree(path, store) -> ImportReport
gc(store, mode="drop-pruned") -> GCReport
recompute_charges(store, root_id) -> dict[str, float]
redact(value, hint=None) -> dict
```

`SQLiteSealSink(path).publish(report, store_id=..., signer_identity=...)`
appends a `SealCustodyRecord` to a database kept outside the Pollard store.

`verify` checks the selected node and its ancestry. The CLI walks every node in
the selected root when performing a whole-tree verification. `seal` raises on
invalid nodes and produces a rolling digest over node IDs and result digests.
`merge` unions disconnected stores; replay mode rejects result conflicts.
Merge is not a cross-node or cross-backend transaction, so a failure can retain
changes already accepted by the destination. Repeating the exact merge is
idempotent. CLI merge prepares every source into a private disk-backed spool
before destination access and bounds its aggregate preparation working set in
RAM. Concurrent destination writers can race metadata updates; quiesce them for
an evidence-grade transfer.
Export includes a complete seal, and import verifies it before any write.
Garbage collection is explicit and offline; supported modes are `drop-pruned`
and `compact`.

See [Data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md)
and [Seal design](https://github.com/jemsbhai/pollard/blob/main/docs/seal.md)
before using an exported tree as evidence.

## Async calls

`AsyncRun` inherits cursor, note, branch, rollback, prune, and report behavior.
Use:

```python
await run.amodel_call(payload, fn=async_model)
await run.atool_call(name, args, fn=async_tool)
await run.aconfirm(resume_token)
```

An async step may resolve to a dictionary, synchronous iterator, or async
iterator. Async provider adapters are available for OpenAI, Anthropic, and
LiteLLM where the surrounding SDK exposes an async client. The Bedrock adapter
currently wraps the synchronous boto3 client.

## Provider and framework modules

| Module | Public integration functions |
|---|---|
| `pollard.adapters.openai` | Responses and Chat Completions, sync and async |
| `pollard.adapters.anthropic` | Messages, sync and async, including live input-token estimator on the sync callable |
| `pollard.adapters.bedrock` | Synchronous boto3 Converse and ConverseStream, optional CountTokens |
| `pollard.adapters.litellm` | Completion and async completion wrappers |
| `pollard.mcp` | Build a frozen Pollard registry from a caller-owned MCP session |
| `pollard.otel` | Export stored nodes or attach a content-free live node callback |

The complete commands, credentials, cost limits, outputs, and framework
boundaries are in the
[integration recipe index](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md).

Direct adapter results expose normalized `usage` for meters and preserve the
original provider object as `provider_usage`. `OpenAIResponseError`,
`AnthropicStreamError`, and `BedrockStreamError` are module-level structured
errors for terminal provider events or streams that close without their
required terminal event.

## Exceptions

All Pollard exceptions derive from `PollardError`:

| Exception | Meaning | Useful field |
|---|---|---|
| `BudgetExceeded` | Precheck recorded a budget or window refusal | `refusal_id` |
| `PolicyViolation` | Registry or policy recorded a refusal | `refusal_id` |
| `ConfirmationRequired` | Policy requires explicit continuation | `resume_token` |
| `MissingRecording` | Strict replay found no stored result | `node_id`, `payload_summary` |
| `IntegrityError` | Stored or transferred data failed integrity validation | Exception message |
| `PostDispatchOutcomeUnknown` | A caller explicitly reports an external call with unknown outcome | `error` |
| `CallCleanupError` | Secondary cleanup errors chained behind a primary call error | `errors` |
| `ReservationLeaseLost` | A completed call lost its shared reservation lease | `reservation_id`, `node_id`, `detail` |
| `ReservationUncertain` | Reserve or release could not be confirmed after reconnect | `reservation_id` |
| `SettlementUncertain` | A completed call's shared settlement could not be confirmed | `reservation_id` |
| `UnsupportedSchema` | Action schema uses an unsupported keyword or type | Exception message |

Provider SDK, tool handler, callback, filesystem, and database exceptions are
not converted into successful Pollard results. Consult
[Troubleshooting](https://github.com/jemsbhai/pollard/blob/main/docs/troubleshooting.md)
for diagnostic paths and safe issue data.

Direct provider adapters mark generation failures after dispatch without
replacing the native exception type. Generic callables can use
`mark_post_dispatch_outcome_unknown(error)` or raise
`PostDispatchOutcomeUnknown(error)`. The runtime settles available precheck
estimates, records a content-free failure note, and re-raises the original
error. `is_post_dispatch_outcome_unknown(error)` inspects either representation.
Token-count failures occur during precheck and remain ordinary errors.

The same post-dispatch rule covers `KeyboardInterrupt`, `SystemExit`, and
asynchronous cancellation. If a completed result has missing or invalid usage,
a meter marked with `precheck_is_estimate` settles its reservation estimate and
the node records `accounting_fallbacks`; valid provider usage remains
authoritative.
