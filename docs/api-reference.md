# Public API reference

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
| `WindowMeter(name, limit, window_seconds, meter=None)` | Wrapped meter charge in a shared sliding window | Uses wrapped meter estimate |

`CostMeter` price rows require `input_per_1m` and `output_per_1m`. Pricing is
caller data and must be updated when a provider price changes. A settled
dollar charge is not a provider-account hard limit.

Optional meters include `EnergyMeter` in `pollard.meters.energy` and
`TokenmasterMeter` in `pollard.meters`. The OpenAI prompt estimator is
`pollard.estimators.openai.OpenAITokenEstimator`.

## Registry and policies

```python
ActionSpec(name, version, description, schema, side_effects, handler=None)
Registry([spec, ...])
```

An action spec computes `spec_digest` from every field except the handler. A
registry rejects duplicate names and computes `registry_digest` from its sorted
spec digests. A run root binds to one registry digest.

The zero-dependency schema subset accepts `type`, `properties`, `required`,
`enum`, `items`, `additionalProperties`, and Pollard's `sensitive` marker. Types
are object, string, integer, boolean, array, and null. Unsupported keywords or
types raise `UnsupportedSchema` when the spec is constructed.

`sensitive: true` is valid on string fields. Pollard validates the original
argument, supplies it to policies and the handler, but hashes and stores a
redaction marker. Handler results and metadata are not automatically redacted.

A policy implements:

```python
def decide(self, ctx: PolicyContext) -> Decision: ...
```

`PolicyContext` contains the resolved spec, original arguments, cursor ID, run
label, and current settled counters.

## Replay modes

`ReplayMode.RECORD` always executes a step function and stores its result.
`ReplayMode.HYBRID` reuses an exact existing result or executes on a miss.
`ReplayMode.REPLAY` never executes and raises `MissingRecording` on a miss.

Replay validates stored ancestry before serving a result. Identity includes the
parent, kind, payload, and attempt, so equivalent payloads beneath different
parents are distinct steps.

## Stores

| Store | Constructor | Intended scope |
|---|---|---|
| `MemoryStore` | `MemoryStore()` | Tests and one-process ephemeral runs |
| `SQLiteStore` | `SQLiteStore(path, intern_payloads=True, intern_threshold=1024)` | Persistent one-host runs and moderate process sharing |
| `PostgresStore` | `PostgresStore(conninfo, store_id="default", ...)` | Transactional multi-process and multi-host runs |
| `HashRopeStore` | `HashRopeStore(data=b"")` | In-process operation log and byte snapshot |

`Store` is the frozen structural protocol with `put`, `get`, `exists`,
`children`, `update_meta`, `walk`, and `roots`. Custom stores must preserve
content-addressed identity, parent existence, deterministic child order, and
the documented method meanings.

SQLite and PostgreSQL intern large string payload leaves by default. Interning
is a storage encoding, not redaction or encryption. PostgreSQL requires the
`pg` extra. Hashrope requires the `hashrope` extra.

`PostgresStore.migrate(conninfo)` performs the explicit legacy-to-current
schema migration and returns `(old_version, new_version)`. It requires a drained
reservation table. `store.reconnect()` replaces a broken connection and checks
the schema version before returning.

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

## Exceptions

All Pollard exceptions derive from `PollardError`:

| Exception | Meaning | Useful field |
|---|---|---|
| `BudgetExceeded` | Precheck recorded a budget or window refusal | `refusal_id` |
| `PolicyViolation` | Registry or policy recorded a refusal | `refusal_id` |
| `ConfirmationRequired` | Policy requires explicit continuation | `resume_token` |
| `MissingRecording` | Strict replay found no stored result | `node_id`, `payload_summary` |
| `IntegrityError` | Stored or transferred data failed integrity validation | Exception message |
| `ReservationLeaseLost` | A completed call lost its shared reservation lease | `reservation_id`, `node_id` |
| `ReservationUncertain` | Reserve or release could not be confirmed after reconnect | `reservation_id` |
| `SettlementUncertain` | A completed call's shared settlement could not be confirmed | `reservation_id` |
| `UnsupportedSchema` | Action schema uses an unsupported keyword or type | Exception message |

Provider SDK, tool handler, callback, filesystem, and database exceptions are
not converted into successful Pollard results. Consult
[Troubleshooting](https://github.com/jemsbhai/pollard/blob/main/docs/troubleshooting.md)
for diagnostic paths and safe issue data.
