# pollardai

Experimental **0.1.0 native TypeScript core** of [Pollard](https://github.com/jemsbhai/pollard), for Node.js 20+. It has no Python dependency and no runtime npm dependencies. Both ESM and CommonJS builds include TypeScript declarations.

This first release supports content-addressed execution trees, detached in-memory storage, integrity verification, record/hybrid/replay modes, synchronous and asynchronous calls, integer step/token/depth budgets, branches and rollback, a frozen action registry, synchronous policies, confirmation and sensitive-argument redaction. It is a subset of the Python package, with a separate experimental version.

```sh
npm install pollardai
```

```ts
import { MemoryStore, Runtime } from 'pollardai';

const store = new MemoryStore();
const runtime = new Runtime({ store });
const run = runtime.run('first-run', {
  budget: { steps: 2, tokens: 20, depth: 4 },
});

const node = run.modelCall(
  { model: 'local-demo', prompt: 'hello' },
  () => ({
    text: 'Hello',
    usage: { input_tokens: 2, output_tokens: 3 },
  }),
  { tokenEstimate: 10 },
);

console.log(node.id, node.result, run.report());

const replay = new Runtime({ store, mode: 'replay' }).run('first-run');
const saved = replay.modelCall(
  { model: 'local-demo', prompt: 'hello' },
  () => { throw new Error('Replay never invokes this handler'); },
);
console.log(saved.id === node.id); // true
```

For an asynchronous handler, use `await run.modelCallAsync(payload, handler, options)` or `await run.toolCallAsync(name, args, handler, options)`. Handlers return a JSON object. Inputs, stored nodes, registry schemas and policy contexts are frozen snapshots; do not mutate them.

## Execution and budgets

- `mode: 'record'` always rejects an existing call identity with `DuplicateRecordingError`. Use `{ attempt: 1 }` for a deliberate retry. The default attempt is zero.
- `mode: 'hybrid'` reuses a verified completed recording and executes missing identities. It refuses pending, failed and dry-run recordings.
- `mode: 'replay'` requires every exact structure and completed call recording, verifies identity/result integrity, and executes no handlers. A changed payload or attempt produces `MissingNodeError`.
- `run.note(payload)` records a structural node. `run.branch({ budget, attempt })` returns a child `Run` with its own cursor. Ancestor budgets include spending in every branch. `run.rollback(targetId)` or `run.rollback(undefined, steps)` moves to an ancestor; spending is never refunded.
- Budget fields, attempts, estimates and usage counts must be non-negative safe integers. Depth is the next node's distance from the root, including notes and branch anchors. Depth limits apply globally, including a branch's local budget.
- A token budget requires a per-call `tokenEstimate` or a configured `estimateTokens(payload)` callback. `reservedOutputTokens` adds to model estimates. Explicit zero estimates are allowed. Estimates are pre-dispatch checks; actual provider usage can overshoot. The completed result is retained and subsequent calls are blocked when spending exceeds the limit, including new handles opened with the same run label.
- Usage has the shape `{ input_tokens: number, output_tokens: number }`. Missing usage under a token budget, or invalid supplied usage, raises `UsageError` after recording the completed result. It marks accounting unknown and blocks later token-budget calls. Such completed results remain replayable. Missing usage without a token budget records zero or estimated token charges and marks accounting unknown.

Before dispatch, a pending record reserves one step and estimated tokens. A handler failure or unusable result leaves a failed record with unknown accounting. It cannot be redispatched at the same identity. A new attempt can retry; token-budget execution remains blocked while accounting is unknown. No external side effect can be rolled back by this library.

Calls and policy/estimator callbacks are serial within a store object in one process. Concurrent or reentrant runtime operations throw `ConcurrentCallError`. This release has no distributed transactional arbiter; distinct store objects or processes must not perform concurrent writes to the same backing data.

## Registered actions and policies

```ts
import { ActionSpec, ConfirmationRequired, Registry, Runtime } from 'pollardai';

const registry = new Registry([
  new ActionSpec({
    name: 'send', version: '1', description: 'Send a notification',
    sideEffects: true,
    schema: {
      type: 'object',
      properties: {
        message: { type: 'string' },
        apiKey: { type: 'string', sensitive: true },
      },
      required: ['message', 'apiKey'], additionalProperties: false,
    },
    handler: args => ({ accepted: true }),
  }),
]);

const run = new Runtime({
  registry,
  policies: [{ decide: ctx => ctx.spec.sideEffects ? 'confirm' : 'allow' }],
}).run('notifications', { budget: { steps: 1 } });

try {
  run.toolCall('send', { message: 'Hello', apiKey: 'example' });
} catch (error) {
  if (!(error instanceof ConfirmationRequired)) throw error;
  // Present the action to your user, then call this after their approval:
  const node = run.confirm(error.token);
  console.log(node.payload.args); // apiKey is a content-committing redaction marker
}
```

`toolCall(name, args, handler?, { version, attempt, tokenEstimate })` uses only the registered handler when a registry is present. Unknown tools, mismatched versions, invalid arguments, missing handlers and policy denials record refusal nodes and raise `PolicyViolation`. Registries bind their digest to a run root, so a different registry cannot reopen that run.

Policies synchronously return `'allow'`, `'deny'` or `'confirm'`; denial takes precedence. Confirmation tokens are scoped to their run handle and parent cursor, hold immutable original arguments, and are single use. Use `confirmAsync(token)` for an asynchronous registered handler. `dryRun: true` suppresses side-effect handlers and records an auditable dry-run node charging one step; it also applies after confirmation. Dry-run nodes are not executable replay results.

The supported schema subset is: `type` (`object`, `string`, `integer`, `boolean`, `array`, `null`), `properties`, `required`, `enum`, `anyOf`, `items`, integer `minimum`/`maximum`/exclusive bounds, `minLength`/`maxLength`, `minItems`/`maxItems`, boolean `additionalProperties`, `title`, `description`, `default` (annotation only), and `sensitive` for strings or nullable strings. String lengths count Unicode scalar values. Unsupported keywords, including `$ref`, fail closed with `UnsupportedSchema`.

Sensitive string arguments are replaced only in audit payloads by Pollard's deterministic `redact(value, hint?)` marker. The handler receives the original frozen values. Results are not automatically redacted; return only the data you want retained. Deterministic markers are content commitments, not encryption.

## Identity, storage and interoperability

`canonicalText`, `canonicalBytes`, `nodeId`, `digestPayload`, `resultDigestFromText`, `redact`, `Node` and `verify` expose the core primitives. Node identity uses the frozen `pollard/v1\n` domain and matches Python on the shared portable subset: null, booleans, Unicode scalar strings, arrays, plain string-keyed objects and integers in `[-9007199254740991, 9007199254740991]`. Keys are ordered by Unicode scalar value, including integer-looking keys. Floats, unsafe integers, nonplain or proxy objects, accessors, hidden/symbol fields, sparse arrays, cycles and lone surrogates are rejected in identities.

`Node.make({ kind, parent, payload, attempt?, result?, meta? })` creates an immutable node. `Node.fromStorage(record)` imports `id`, `kind`, `parent`, `attempt`, `payload`, `result_text`, `result_digest` and `meta`; `node.toStorage()` returns a detached storage object. `verify(store, nodeId)` checks that node and its ancestry, including fetched-node lookup binding.

Results permit finite floating-point JSON values. Imported `result_text` is preserved byte-for-byte and its digest uses `pollard/v1:result\n` plus that exact UTF-8 text. Native JavaScript result serialization can differ from Python for floating-point spellings (for example, Python `2.0` versus JavaScript `2`); do not regenerate imported text when verifying hashes. Imported result values must fit JavaScript's safe numeric subset. This release does not import Python SQLite databases or other store file formats.

The frozen `Store` interface contains seven methods: `put`, `get`, `exists`, `children`, `updateMeta`, `walk`, `roots`. Metadata patches merge shallowly and return detached values. `MemoryStore` implements those methods and the optional `RecordingStore.finalize(node)` extension, which completes a pending dispatch once while preserving its identity. Custom seven-method stores can replay existing recordings; live execution additionally requires a correct `finalize` implementation. Treat custom stores and their mutable metadata as trusted infrastructure. Integrity hashes cover identities and exact result text, not metadata or proof that a handler ran.

## Scope and development

This release omits Python's streaming, persistent/distributed store backends, lease/reservation protocols, cost/wall-clock/window/custom meters, revalidation, framework/provider integrations, CLI and MCP integrations. It provides neither browser support nor full Python feature parity.

```sh
npm ci
npm test
npm pack --dry-run
npm run smoke:pack
```

Tests include the Python-generated interoperability fixture in `test/vectors.json`, replay safety, duplicate prevention, pre-dispatch budgets and actual settlement, immutable callback snapshots, policy/confirmation behavior, concurrency refusal and store integrity attacks. MIT licensed.
