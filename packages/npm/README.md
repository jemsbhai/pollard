# pollardai

Native TypeScript governed execution trees for Node.js 20+: budget, gate, record,
stream, replay, and audit caller-owned model and tool calls. Both ESM and CommonJS
builds include TypeScript declarations. The core has no runtime dependencies and
does not launch Python.

This checkout prepares **0.2.0**, with behavior checked against **PyPI Pollard
1.6.0**. The already published npm release is 0.1.0; the additions below become
available when 0.2.0 is published, or by installing this checkout's packed archive.
The Rust package retains its independent 0.1.0 scope.

## First run

```js
import { Runtime } from 'pollardai';

const run = new Runtime().run('first-run', { budget: { steps: 1, tokens: 10 } });
const node = run.modelCall(
  { model: 'local-demo', prompt: 'hello' },
  payload => ({
    text: `offline reply for ${payload.prompt}`,
    usage: { input_tokens: 2, output_tokens: 4 },
  }),
);
console.log(node.result.text); // offline reply for hello
console.log(run.report().spent.tokens); // 6
```

Inputs, nodes, schemas, and policy contexts are immutable snapshots. Identical
inputs, ancestry, and attempt numbers produce the same IDs as Python on the
portable identity subset. Budget and timing metadata do not affect node identity.

## Streaming and replay

```js
import { MemoryStore, Runtime } from 'pollardai';

const store = new MemoryStore();
const run = new Runtime({ store }).run('stream');
await run.modelCallAsync({ model: 'offline' }, async function* () {
  yield { delta: { text: 'Hello ' } };
  yield { delta: { text: 'world' } };
  yield { usage: { input_tokens: 2, output_tokens: 3 } };
}, { keepChunks: true, onDelta: chunk => console.log(chunk) });

const replay = new Runtime({ store, mode: 'replay' }).run('stream');
await replay.modelCallAsync({ model: 'offline' }, () => {
  throw new Error('Replay never calls this');
}, { onDelta: chunk => console.log(chunk) });
```

Synchronous handlers may return an iterable; asynchronous handlers may return
an async iterable. Chunks accept `delta`, `usage`, and a final `result`. Charges
settle once. `keepChunks` stores detached chunks and replay re-emits them.
`onDelta` may return a promise on async calls. A rejected callback, incomplete
provider stream, or cancellation preserves failure evidence and conservative
charges. `signal: AbortSignal` prevents pre-dispatch execution and retains the
store lock until any already-started provider promise settles.

`mode: 'hybrid'` reuses verified completed results and executes missing calls.
`mode: 'replay'` never dispatches. `mode: 'record'` always rejects an existing
identity with `DuplicateRecordingError`; use a new `attempt` for an intentional
retry. This stronger default from npm 0.1 is retained: Python 1.6's duplicate
guard is opt-in. Pending, failed, and dry-run records are never executable replay
results.

## Budgets and meters

Defaults are `StepMeter`, `DepthMeter`, `WallClockMeter`, and `TokenMeter`.
`Runtime({ meters })` replaces that list. Budgets accept `steps`, `tokens`,
`depth`, `seconds`, `usd`, and `extra: { customMeter: limit }`. Custom meters
implement `name`, `precheckEstimate(kind, payload)`, and
`charge(kind, payload, result, meta)`. A measurement may expose
`measure(): { start(), stop(), readings() }`.

```js
import { Runtime, StepMeter, TokenMeter, CostMeter, WindowMeter } from 'pollardai';

const runtime = new Runtime({ meters: [
  new StepMeter(),
  new TokenMeter({ estimator: payload => 10, reservedOutputTokens: 32 }),
  new CostMeter({ 'my-model': { inputPer1m: 1, outputPer1m: 4 } }),
  new WindowMeter('requests', 60, 60),
] });
const run = runtime.run('metered', { budget: { steps: 100, tokens: 5000, usd: '0.25' } });
```

Price values above are caller-supplied examples, not a provider price list.
Known estimates refuse before dispatch. Actual usage can exceed an estimate;
completed results remain recorded and subsequent calls are refused. Missing or
invalid usage uses an available estimate conservatively and records the fallback.
Without an estimate it charges zero, as Python does; this cannot establish a
hard token ceiling. Unlike 0.1, a token budget does not require an estimator and
missing usage does not raise `UsageError`.

`OpenAITokenEstimator` uses the optional `js-tiktoken` peer or an injected
`Tokenizer`. `EnergyMeter({ nvml })` accepts an explicit local NVML binding and
prefers cumulative energy counters, falling back to power samples. Power sampling
uses the Node event loop; blocking synchronous computation reduces sample density.
It measures the whole local GPU, not hosted provider energy.

`TokenmasterMeter`, `TokenmasterCostMeter`, and `tokenmasterGovernanceMeters`
accept an explicit synchronous `TokenmasterClient`. They provide profile refusal,
exclusive cache/reasoning accounting, conservative tier quotes, diagnostics, and
USD fallback settlement. No Python tokenmaster module or built-in price registry
is bundled; applications supply the profile/quote implementation described by the
exported interfaces. Profile enforcement is opt-in and requires an estimator.

`run.branch({ budget, attempt })` creates an independent cursor sharing ancestor
spending. `rollback(targetId)` moves to an ancestor without refunding charges.
`prune()` marks the current node as pruned; explicit offline `gc()` removes its subtree.
`runtime.resume(label, options)` resumes stored unpruned work. `onNode` receives
persisted nodes and does not replace a provider outcome if the observer fails.

## Registered tools, JSON Schema, and MCP

```js
import { ActionSpec, Registry, Runtime } from 'pollardai';

const registry = new Registry([new ActionSpec({
  name: 'lookup', version: '1', description: 'Look up a record', sideEffects: false,
  schema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'], additionalProperties: false },
  handler: args => ({ found: args.id }),
})]);
const run = new Runtime({ registry }).run('tools');
run.toolCall('lookup', { id: 'example' });
```

Policies synchronously decide `allow`, `deny`, or `confirm`; denial takes
precedence. `ConfirmationRequired.token` is single-use and bound to the original
cursor and immutable arguments. Use `confirm` or `confirmAsync` after obtaining
approval. `dryRun: true` suppresses side-effect handlers and records a dry-run
node. Dry runs do not charge dispatch meters.

Sensitive strings are redacted in audit payloads with deterministic commitments;
handlers receive originals. Results are not automatically redacted. Registry
digests bind a root to its action set. Local `$ref`/`$defs` and `definitions`
resolve before validation and hashing. Cycles, remote references, unsupported
keywords, and constrained reference siblings fail closed. The supported schema
subset includes objects, arrays, integers, strings, booleans, null, `anyOf`,
`enum`, required fields, bounds, and boolean `additionalProperties`.

`await registryFromMCP(session, { exclude })` supports the JavaScript MCP SDK's
`listTools`/`callTool`, pagination, generated local-reference schemas, and explicit
side-effect policy gating. The application owns the MCP session and credentials.

## Providers and revalidation

Provider factories consume existing clients: `makeResponsesFn`,
`makeChatCompletionsFn` (also Azure OpenAI), `makeMessagesFn` (Anthropic),
`makeConverseFn` (AWS BedrockRuntime aggregate client or command wrappers), and
`makeCompletionFn` (an injected compatible completion function/LiteLLM proxy).
Use these functions with `modelCallAsync`. Each accepts `{ stream, defaults }`.
They normalize text, tool calls, and usage while retaining `provider_usage`.
They never create clients, read credentials, or set retry policies.

```js
import { Runtime, makeResponsesFn } from 'pollardai';
// client is an application-owned OpenAI/Azure OpenAI SDK instance.
const call = makeResponsesFn(client, { stream: true, defaults: { store: false } });
const run = new Runtime().run('live', { budget: { tokens: 10000 } });
const node = await run.modelCallAsync(
  { model: 'your-model', input: 'Hello', max_output_tokens: 128 }, call,
  { tokenEstimate: 200, keepChunks: true },
);
```

Anthropic and Bedrock factories expose `estimateInputTokens(payload)` as an
explicit async network request; await it before supplying a `tokenEstimate`.
Bedrock counting additionally requires `{ countTokens: true }`. Local estimators
remain synchronous. SDK calls can incur charges; the application owns retries
and provider idempotency.

`ReplayContract` binds provider/model/environment revisions into input identity.
`run.revalidateModelCall` and `revalidateModelCallAsync` make a separate budgeted
live observation and preserve the original result. `ExactResultComparator` and
`NormalizedModelComparator` produce value-free JSON-pointer differences. Strict
replay never invokes live revalidation. See the exported `RevalidationOptions`
for contracts, observation IDs, comparators, and an optional replacement payload.

## Stores and governance

| Store | Optional npm peer | Coordination |
|---|---|---|
| `MemoryStore` | none | One process/store object |
| `SQLiteStore(path)` | `better-sqlite3` | Atomic budgets and windows on one host |
| `HashRopeStore` | none | In-process Python-compatible operation log and polynomial hash |
| `PostgresStore(url, options)` | `pg` | Serializable namespace transactions |
| `RedisStore(url, options)` | `redis` | WATCH/MULTI namespace transactions |
| `MongoStore(url, options)` | `mongodb` 6 | Replica-set/sharded transactions |
| `Neo4jStore(url, options)` | `neo4j-driver` | Primary-routed graph transactions |
| `KafkaStore(options)` | `kafkajs` | Ordered audit/replay, no shared budget arbiter |

Install only the selected peers. Always close persistent and remote stores.
For SQLite, install `better-sqlite3@11.10.0` on Node 20, or
`better-sqlite3@13.0.3` on Node 22 and newer. Version 13 requires Node 22+.
Node 24+ requires version 13.0.3+ because older drivers can abort the process
during native statement cleanup with recent Node build headers
([upstream issue](https://github.com/nodejs/node/issues/65446)). Pollard checks
driver compatibility before loading the native addon. Importing Pollard or
using another store does not require a SQLite driver.

Remote options include `storeId`, `create` (false opens an existing namespace),
and `timeoutMs`. Mongo adds `database`/`prefix`; Neo4j requires
`username`/`password` and optional `database`; Kafka requires `brokers` and `topic`
and accepts KafkaJS `clientConfig`. Redis requires durable persistence and
`noeviction`. Kafka requires one dedicated partition, infinite byte/time retention,
and deletion-only cleanup (no compaction).

```js
import { Runtime, SQLiteStore } from 'pollardai';
const store = new SQLiteStore('runs.db');
try {
  const run = new Runtime({ store }).run('saved', { budget: { steps: 10 } });
  run.modelCall({ model: 'offline' }, () => ({ text: 'saved' }));
} finally { store.close(); }
```

SQLite v3 recordings, exact imported result text, sealed JSON subtree manifests,
external SQLite seal custody, and completed HashRope logs interoperate with
Python. Native remote stores use an isolated npm namespace/schema; transfer
recordings through `exportSubtree`/`importSubtree` rather than pointing Python and
Node at the same remote physical tables. Mixed Python/Node live remote budgets
are not coordinated. All workers sharing a native budget must use the same
backend and store ID.

Remote I/O runs in dedicated Node workers, keeping the synchronous Store API and
renewing leases during blocking application code. The initial native backend
stores the full logical namespace as one transactional state document and
validates it on operations. This favors correctness over large-store throughput;
MongoDB's document limit also bounds one namespace. It is not a performance
replacement for Python's row-oriented stores. Use separate store IDs to bound
namespace size. RPC timeouts close the handle and report an uncertain outcome;
reopen and reconcile before retrying. Do not assume an exception rolled back an
external provider or database commit.

`seal`/`verifySeal`, `SQLiteSealSink`, `exportSubtree`/`importSubtree`,
`exportJSONL`/`importJSONL`, `merge`, and `gc` implement audit and maintenance.
`merge(destination, source, { requireAtomic: true })` refuses a destination
without transaction support. Native memory/SQLite/transactional remote stores
support atomic maintenance; concurrent remote updates abort an optimistic
maintenance transaction without retrying its callback. Kafka supplies ordered
single operations and rejects atomic maintenance. GC is an offline operation:
drain writers first. JSONL is an npm envelope; Python exchanges use sealed JSON.

## CLI and telemetry

The `pollardai` executable supplies `runs`, `show`, `report`, `verify`, `seal`,
`export`, `import`, `merge`, and `gc`. `show --html FILE` creates a self-contained
escaped report. Payloads are excluded unless `--include-payloads` is requested.
Read commands do not create databases. Run `pollardai --help` for argument syntax
and credential-safe environment-backed remote specifications. Remote CLI specs
retain Python's `pg-env:`, `redis-env:`, `mongo-env:`, `neo4j-env:`, and `kafka-env:`
forms; Kafka config JSON uses KafkaJS keys. Direct credential-bearing URLs are
not accepted by this CLI. CLI GC is SQLite-only.

`spanAttributes`, `exportSpans(store, rootId, tracer, telemetryAPI?)`, and
`liveSpanHook(tracer)` export content-free OpenTelemetry data. Offline spans
preserve parent contexts. Pass an API explicitly or install `@opentelemetry/api`.
Prompts, arguments, and result contents are omitted.

## Compatibility and development

The frozen identity domains remain `pollard/v1\n` and `pollard/v1:result\n`.
Identity data permits JSON null/booleans/Unicode-scalar strings/arrays/plain
objects and safe integers. Floats, unsafe integers, proxies, accessors, sparse
arrays, cycles, and lone surrogates are rejected. Python accepts larger integers.
Result values allow finite floats within JavaScript's numeric constraints;
imported `result_text` is preserved byte-for-byte. Native JS float serialization
can differ from Python. Metadata is trusted mutable infrastructure outside the
identity/result hashes. Hashes detect altered content, not deletion of a store.

JavaScript uses `Runtime` with async methods in place of a separate Python
`AsyncRuntime`, and Node's test runner in place of the pytest plugin. Python-only
Pydantic/framework packages are not bundled; use their JavaScript equivalents
through caller-owned functions and portable JSON Schema. See
[the parity matrix](https://github.com/jemsbhai/pollard/blob/main/docs/npm-parity.md)
for verified coverage and deliberate differences.

```sh
npm ci
# On Node 24+, replace the Node 20 development driver before running tests:
# npm install --no-save --package-lock=false better-sqlite3@13.0.3
npm test
npm run smoke:pack
npm run test:remote
```

`test:remote` starts isolated Docker Compose services and removes that test
project afterward. Set `POLLARD_DOCKER_CONTEXT=desktop-linux` when needed.
Python fixture checks run from the repository root:

```sh
python interop/generate_vectors.py --check --check-packages
python interop/generate_npm_parity.py --check
```

The tests cover Python-produced golden values, bidirectional SQLite/manifest
exchange, real distributed service races, pre-dispatch refusal, immutable callback
snapshots, streaming completion/cancellation, settlement uncertainty, and clean
ESM/CommonJS/TypeScript tarball consumers. MIT licensed.
