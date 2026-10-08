import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { ActionSpec, BudgetExceeded, canonicalBytes, canonicalText, ConcurrentCallError, ConfirmationRequired, digestPayload, DuplicateRecordingError, IntegrityError, MemoryStore, MissingNodeError, Node, nodeId, PolicyViolation, redact, Registry, resultDigestFromText, Runtime, UnsupportedSchema, UsageError, verify } from '../dist/esm/index.js';

const vectors = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url), 'utf8'));
const cjs = createRequire(import.meta.url)('../dist/cjs/index.js');
const reply = (tokens = 2) => ({ text: 'hello', usage: { input_tokens: tokens, output_tokens: 0 } });
const spec = (handler = args => ({ text: args.text })) => new ActionSpec({ name: 'echo', version: '1', description: 'Echo', sideEffects: false, schema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'], additionalProperties: false }, handler });
const storage = value => { const { result, ...record } = value; return record; };

test('all Python canonical bytes and frozen node vectors', () => {
  for (const v of vectors.canonical_cases) {
    assert.equal(canonicalText(v.value), v.canonical_text, v.name);
    assert.deepEqual(canonicalBytes(v.value), Buffer.from(v.canonical_text, 'utf8'), v.name);
  }
  for (const v of vectors.node_cases) {
    assert.equal(canonicalText({ a: v.attempt, k: v.kind, p: v.parent ?? '', pl: v.payload }), v.canonical_identity_text, v.name);
    assert.equal(nodeId(v.kind, v.parent, v.attempt, v.payload), v.id, v.name);
    assert.equal(Node.make(v).id, v.id, v.name);
  }
});

test('all Python rejection vectors and unsafe JavaScript identities', () => {
  for (const v of vectors.rejected_cases) {
    if (v.operation === 'canonical_bytes') {
      const value = v.integer_text !== undefined ? Number(v.integer_text) : v.json_text !== undefined ? JSON.parse(v.json_text) : v.value;
      assert.throws(() => canonicalText(value), undefined, v.name);
    } else assert.throws(() => Node.make({ kind: v.kind ?? 'root', parent: Object.hasOwn(v, 'parent') ? v.parent : null, payload: {}, attempt: v.attempt ?? 0 }), undefined, v.name);
  }
  const cyclic = {}; cyclic.x = cyclic;
  const hidden = {}; Object.defineProperty(hidden, 'x', { value: 1 });
  const accessor = {}; Object.defineProperty(accessor, 'x', { get() { throw Error('should never run'); }, enumerable: true });
  for (const value of [NaN, Infinity, 1.5, undefined, () => 1, new Date(), new Map(), cyclic, [, 1], { [Symbol('x')]: 1 }, { x: undefined }, hidden, accessor, '\udfff']) assert.throws(() => canonicalText(value));
});

test('exact imported result text is retained and digested without reserialization', () => {
  const root = Node.make({ kind: 'root', parent: null, payload: { run: 'results' } });
  for (const v of vectors.result_cases) {
    assert.equal(resultDigestFromText(v.result_text), v.result_digest, v.name);
    const candidate = Node.make({ kind: 'model_call', parent: root.id, payload: { case: v.name } });
    const node = Node.fromStorage({ ...candidate.toStorage(), result_text: v.result_text, result_digest: v.result_digest });
    assert.equal(node.resultText, v.result_text);
    assert.deepEqual(node.result, v.result);
    assert.throws(() => Node.fromStorage({ ...node.toStorage(), result_text: '{"tampered":true}' }), IntegrityError);
  }
});

test('Python registry digests, redaction markers, audit payloads and node identities', () => {
  const specs = vectors.registry.specs.map(v => new ActionSpec({ ...v.identity, sideEffects: v.identity.side_effects, handler: () => reply() }));
  const registry = new Registry(specs);
  assert.equal(registry.registryDigest, vectors.registry.registry_digest);
  for (let i = 0; i < specs.length; i++) assert.equal(specs[i].specDigest, vectors.registry.specs[i].spec_digest);
  for (const v of vectors.redaction_cases) assert.deepEqual(redact(v.value, v.hint), v.marker);
  const runtime = new Runtime({ registry });
  for (const v of vectors.registry.registered_tools) {
    const run = runtime.run('golden');
    const node = run.toolCall(v.name, v.args);
    assert.deepEqual(registry.get(v.name).redactArgs(v.args), v.audit_args);
    assert.deepEqual(node.payload, v.node.payload);
    assert.equal(node.id, v.node.id);
    assert.equal(canonicalText({ a: node.attempt, k: node.kind, p: node.parent, pl: node.payload }), v.node.canonical_identity_text);
  }
});

test('detached store fixture, shallow patches and tamper detection', () => {
  const store = new MemoryStore();
  const rootInput = storage(vectors.detached_store.root_before);
  const root = Node.fromStorage(rootInput); store.put(root);
  rootInput.payload.nested.flag = false; rootInput.meta.nested.value = 'changed';
  assert.deepEqual(store.get(root.id).toStorage(), storage(vectors.detached_store.root_after_mutations));
  const detached = store.get(root.id).toStorage(); detached.meta.nested.value = 'changed';
  assert.equal(store.get(root.id).meta.nested.value, 'original');
  const patch = { nested: { replacement: true } }; store.updateMeta(root.id, patch); patch.nested.replacement = false;
  assert.deepEqual(store.get(root.id).toStorage(), storage(vectors.detached_store.root_after_shallow_patch));
  const child = Node.fromStorage(storage(vectors.detached_store.child)); store.put(child);
  assert.equal(verify(store, child.id).ok, vectors.detached_store.verification_ok);
  for (const v of vectors.detached_store.tamper_cases) assert.throws(() => Node.fromStorage({ ...child.toStorage(), [v.field]: v.replacement }), IntegrityError);
  assert.throws(() => { store.get(root.id).payload.nested.flag = false; }, TypeError);
});

test('lookup binding in a custom Store is checked by verification and replay', () => {
  const backing = new MemoryStore();
  const runtime = new Runtime({ store: backing }); const run = runtime.run('binding');
  const original = run.modelCall({ model: 'one' }, () => reply()); run.rollback();
  const other = run.modelCall({ model: 'two' }, () => reply());
  const wrong = { get: id => id === original.id ? other : backing.get(id), put: n => backing.put(n), exists: id => backing.exists(id), children: id => backing.children(id), updateMeta: (id, patch) => backing.updateMeta(id, patch), walk: id => backing.walk(id), roots: () => backing.roots() };
  assert.equal(verify(wrong, original.id).ok, false);
  assert.throws(() => new Runtime({ store: wrong, mode: 'replay' }).run('binding').modelCall({ model: 'one' }, () => { throw Error('never'); }), IntegrityError);
});

test('completed or failed finalization cannot be rearmed through mutable metadata', () => {
  const store = new MemoryStore(); const root = Node.make({ kind: 'root', parent: null, payload: { run: 'settlement' } }); store.put(root);
  const pending = Node.make({ kind: 'model_call', parent: root.id, payload: {}, meta: { state: 'pending' } }); store.put(pending);
  const complete = Node.make({ kind: 'model_call', parent: root.id, payload: {}, result: reply(), meta: { state: 'completed' } }); store.finalize(complete);
  store.updateMeta(complete.id, { state: 'pending' });
  assert.throws(() => store.finalize(Node.make({ kind: 'model_call', parent: root.id, payload: {}, result: reply(99), meta: { state: 'completed' } })), IntegrityError);
  assert.equal(store.get(complete.id).result.usage.input_tokens, 2);
  const failed = Node.make({ kind: 'model_call', parent: root.id, payload: { failed: true }, meta: { state: 'pending' } }); store.put(failed);
  store.updateMeta(failed.id, { state: 'failed' }); store.updateMeta(failed.id, { state: 'pending' });
  assert.throws(() => store.finalize(Node.make({ kind: 'model_call', parent: root.id, payload: { failed: true }, result: reply(), meta: { state: 'completed' } })), IntegrityError);
});

test('MemoryStore preserves the first result and records conflicting result evidence like Python', () => {
  const store = new MemoryStore(), root = Node.make({ kind: 'root', parent: null, payload: { run: 'result-conflicts' } }); store.put(root);
  const first = Node.make({ kind: 'model_call', parent: root.id, payload: { model: 'same' }, result: { text: 'first' }, meta: { state: 'completed' } });
  const incoming = Node.make({ kind: first.kind, parent: first.parent, payload: first.payload, result: { text: 'second' }, meta: { state: 'pending' } });
  store.put(first); store.put(incoming);
  assert.equal(store.get(first.id).resultText, first.resultText);
  assert.equal(store.get(first.id).meta.state, 'completed');
  assert.deepEqual(store.get(first.id).meta.result_conflicts, [{ result_digest: incoming.resultDigest, result: incoming.result }]);
  assert.throws(() => store.finalize(incoming), IntegrityError);
  const pending = Node.make({ kind: 'tool_call', parent: root.id, payload: { tool: 'pending' }, meta: { state: 'pending' } }); store.claim(pending);
  store.put(Node.make({ kind: pending.kind, parent: pending.parent, payload: pending.payload, result: { forged: true }, meta: { state: 'completed' } }));
  assert.equal(store.get(pending.id).resultText, null); assert.equal(store.get(pending.id).meta.state, 'pending');
});

test('steps, tokens and depth reject before dispatch and retain refusal audit nodes', () => {
  for (const budget of [{ steps: 0 }, { tokens: 1 }, { depth: 0 }]) {
    let dispatched = 0;
    const run = new Runtime().run('precheck', { budget });
    assert.throws(() => run.modelCall({ prompt: 'x' }, () => { dispatched++; return reply(); }, { tokenEstimate: 2 }), BudgetExceeded);
    assert.equal(dispatched, 0); assert.equal(run.cursor.kind, 'refusal');
    assert.equal(run.cursor.payload.blocked_payload_digest, digestPayload({ prompt: 'x' }));
    assert.equal(run.cursor.payload.reason, 'budget');
  }
  for (const budget of [{ tokens: NaN }, { steps: true }, { depth: 1.5 }, { tokens: -1 }, { usd: Infinity }]) assert.throws(() => new Runtime().run('bad-budget', { budget }));
});

test('actual token overshoot preserves result and blocks later calls across run handles', () => {
  const runtime = new Runtime(); const run = runtime.run('overshoot', { budget: { tokens: 3 } });
  const first = run.modelCall({ model: 'first' }, () => reply(5), { tokenEstimate: 1 });
  assert.equal(first.result.usage.input_tokens, 5); assert.equal(run.report().spent.steps, 1); assert.equal(run.report().spent.tokens, 5); assert.ok(run.report().spent.seconds >= 0);
  let dispatched = 0;
  assert.throws(() => run.modelCall({ model: 'second' }, () => { dispatched++; return reply(); }, { tokenEstimate: 0 }), BudgetExceeded);
  const restarted = runtime.run('overshoot', { budget: { tokens: 3 } });
  assert.throws(() => restarted.modelCall({ model: 'new' }, () => { dispatched++; return reply(); }, { tokenEstimate: 0 }), BudgetExceeded);
  assert.equal(dispatched, 0);
});

test('token budgets accept missing estimates and permit explicit zero estimates', () => {
  const run = new Runtime().run('estimate-required', { budget: { tokens: 10 } });
  assert.equal(run.modelCall({}, () => reply()).result.text, 'hello');
  const zero = new Runtime().run('zero-estimate', { budget: { tokens: 0 } });
  assert.equal(zero.modelCall({}, () => reply(0), { tokenEstimate: 0 }).result.usage.input_tokens, 0);
  const invalid = new Runtime({ estimateTokens: () => 0.5 }).run('invalid-estimator');
  assert.throws(() => invalid.modelCall({}, () => { throw Error('never'); }), /non-negative safe integer/);
});

test('options are snapshotted before estimators can mutate identity attempts', () => {
  const options = { attempt: 0 };
  const runtime = new Runtime({ estimateTokens: () => { options.attempt = 1; return 0; } });
  const run = runtime.run('options'); let calls = 0;
  run.modelCall({}, () => { calls++; return reply(); }, { attempt: 1 }); run.rollback();
  options.attempt = 0;
  const firstAttempt = run.modelCall({}, () => { calls++; return reply(); }, options);
  assert.equal(firstAttempt.attempt, 0); assert.equal(calls, 2);
  run.rollback(); options.attempt = 0;
  assert.throws(() => run.modelCall({}, () => { calls++; return reply(); }, options), DuplicateRecordingError);
  assert.equal(calls, 2);
  assert.throws(() => run.modelCall({ other: true }, () => reply(), { unknown: 1 }), /unsupported call option/);
  const accessor = {}; Object.defineProperty(accessor, 'attempt', { enumerable: true, get() { throw Error('must not run'); } });
  assert.throws(() => run.modelCall({}, () => reply(), accessor), /accessor/);
});

test('missing, fractional, boolean, negative and overflow usage settle conservative estimates', () => {
  for (const result of [{ text: 'missing' }, { usage: { input_tokens: 0.5, output_tokens: 1 } }, { usage: { input_tokens: true, output_tokens: 1 } }, { usage: { input_tokens: -1, output_tokens: 1 } }, { usage: { input_tokens: Number.MAX_SAFE_INTEGER, output_tokens: 1 } }]) {
    const runtime = new Runtime(); const run = runtime.run('invalid-usage', { budget: { tokens: 10 } });
    run.modelCall({ model: 'one' }, () => result, { tokenEstimate: 2 });
    assert.equal(run.cursor.meta.accounting_unknown, false);
    assert.equal(run.cursor.meta.charges.tokens, 2);
    assert.equal(run.cursor.meta.accounting_fallbacks.tokens.reason, 'missing_or_invalid_provider_usage');
    assert.equal(run.cursor.resultText !== null, true);
    let called = false;
    assert.throws(() => runtime.run('invalid-usage', { budget: { tokens: 10 } }).modelCall({ model: 'two' }, () => { called = true; return reply(); }, { tokenEstimate: 9 }), BudgetExceeded);
    assert.equal(called, false);
  }
});

test('record never redispatches duplicate calls, including failed outcomes', () => {
  const runtime = new Runtime(); const run = runtime.run('duplicate'); let calls = 0;
  const fn = () => { calls++; return reply(); };
  run.modelCall({ model: 'one' }, fn); run.rollback();
  assert.throws(() => run.modelCall({ model: 'one' }, fn), DuplicateRecordingError);
  run.modelCall({ model: 'one' }, fn, { attempt: 1 }); assert.equal(calls, 2);
  const failed = runtime.run('failure');
  assert.throws(() => failed.modelCall({ model: 'fail' }, () => { calls++; throw Error('external failure'); }));
  assert.equal(failed.report().spent.steps, 1);
  assert.throws(() => failed.modelCall({ model: 'fail' }, fn), DuplicateRecordingError);
  assert.equal(calls, 3);
});

test('hybrid and strict replay return verified results without invoking handlers', () => {
  const store = new MemoryStore(); const recorded = new Runtime({ store }).run('replay');
  const node = recorded.modelCall({ model: 'one' }, () => reply());
  for (const mode of ['hybrid', 'replay']) {
    const run = new Runtime({ store, mode }).run('replay');
    assert.equal(run.modelCall({ model: 'one' }, () => { throw Error('must not dispatch'); }).id, node.id);
    assert.equal(run.report().avoided.steps, 1);
  }
  assert.throws(() => new Runtime({ store, mode: 'replay' }).run('missing'), MissingNodeError);
  assert.throws(() => new Runtime({ store, mode: 'replay' }).run('replay').modelCall({ model: 'changed' }, () => { throw Error('must not dispatch'); }), MissingNodeError);
  const unresolved = new Runtime({ store }).run('unresolved');
  assert.throws(() => unresolved.modelCall({}, () => { throw Error('fail'); }));
  assert.throws(() => new Runtime({ store, mode: 'hybrid' }).run('unresolved').modelCall({}, () => reply()), IntegrityError);
});

test('handler inputs, policy contexts, schemas and registry state are immutable snapshots', () => {
  const payload = { nested: { flag: true } };
  const node = new Runtime().run('immutable').modelCall(payload, arg => {
    payload.nested.flag = false;
    assert.equal(arg.nested.flag, true);
    assert.throws(() => { arg.nested.flag = false; }, TypeError);
    return reply();
  });
  assert.equal(node.payload.nested.flag, true);
  const schema = { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] };
  const action = new ActionSpec({ name: 'echo', version: '1', description: '', sideEffects: false, schema, handler: args => {
    assert.throws(() => { args.text = 'changed'; }, TypeError); return reply();
  } });
  schema.properties.text.type = 'integer'; assert.equal(action.schema.properties.text.type, 'string');
  const policy = { decide: ctx => { assert.throws(() => { ctx.args.text = 'changed'; }, TypeError); return 'allow'; } };
  const runtime = new Runtime({ registry: new Registry([action]), policies: [policy] });
  policy.decide = () => 'deny';
  runtime.run('policy-snapshot').toolCall('echo', { text: 'hello' });
});

test('frozen schema subset fails closed, respects bool/int distinction and Unicode length', () => {
  for (const schema of [{ $ref: '#/x' }, { type: 'number' }, { type: 'string', pattern: 'x' }, { type: 'integer', minimum: 0.5 }, { type: 'integer', sensitive: true }, { enum: [true, true] }]) assert.throws(() => new ActionSpec({ name: 'bad', version: '1', description: '', sideEffects: false, schema }), UnsupportedSchema);
  const action = new ActionSpec({ name: 'bounded', version: '1', description: '', sideEffects: false, schema: { type: 'object', properties: { n: { type: 'integer', minimum: 0, maximum: 2 }, text: { type: 'string', maxLength: 1 } }, required: ['n'], additionalProperties: false }, handler: () => reply() });
  assert.equal(action.validateArgs({ n: 1, text: '😀' }), null);
  for (const args of [{ n: true }, { n: 3 }, {}, { n: 1, other: true }, { n: 1, text: '😀a' }]) assert.notEqual(action.validateArgs(args), null);
});

test('registry gating denies unknown/version/schema actions and binds roots', () => {
  const registry = new Registry([spec()]); const runtime = new Runtime({ registry });
  for (const [name, args, options] of [['missing', {}, {}], ['echo', { text: 'x' }, { version: '2' }], ['echo', { text: true }, {}]]) {
    const run = runtime.run(`gate-${name}-${JSON.stringify(options)}-${JSON.stringify(args)}`);
    assert.throws(() => run.toolCall(name, args, undefined, options), PolicyViolation);
    assert.equal(run.cursor.kind, 'refusal');
  }
  runtime.run('bound');
  const changed = new Registry([new ActionSpec({ name: 'echo', version: '2', description: '', sideEffects: false, schema: {}, handler: () => reply() })]);
  assert.throws(() => new Runtime({ store: runtime.store, registry: changed }).run('bound'), IntegrityError);
});

test('deny precedence, confirmation single use, immutable args and dry-run confirmation', () => {
  let dispatched = 0;
  const action = new ActionSpec({ name: 'send', version: '1', description: '', sideEffects: true, schema: { type: 'object', properties: { secret: { type: 'string', sensitive: true } }, required: ['secret'] }, handler: args => { dispatched++; assert.equal(args.secret, 'initial'); return reply(); } });
  const registry = new Registry([action]); const policies = [{ decide: () => 'confirm' }];
  const run = new Runtime({ registry, policies }).run('confirm');
  const args = { secret: 'initial' }; let token;
  assert.throws(() => run.toolCall('send', args), error => { token = error.token; return error instanceof ConfirmationRequired; });
  args.secret = 'changed'; const result = run.confirm(token); assert.equal(dispatched, 1); assert.deepEqual(result.payload.args.secret, redact('initial'));
  assert.throws(() => run.confirm(token));
  const denied = new Runtime({ registry, policies: [...policies, { decide: () => 'deny' }] }).run('denied');
  assert.throws(() => denied.toolCall('send', { secret: 'initial' }), PolicyViolation);
  const dry = new Runtime({ registry, policies, dryRun: true }).run('dry');
  assert.throws(() => dry.toolCall('send', { secret: 'initial' }), error => { token = error.token; return error instanceof ConfirmationRequired; });
  assert.equal(dry.confirm(token).meta.dry_run, true); assert.equal(dispatched, 1);
});

test('branch spending is shared and rollback never refunds', () => {
  const parent = new Runtime().run('branch', { budget: { steps: 2 } });
  const child = parent.branch({ budget: { steps: 1 } }); const anchor = child.cursorId;
  child.modelCall({ model: 'child' }, () => reply());
  assert.equal(parent.cursorId, parent.rootId);
  assert.throws(() => child.modelCall({ model: 'child-two' }, () => reply()), BudgetExceeded);
  child.rollback(anchor); assert.equal(parent.report().spent.steps, 1);
  parent.modelCall({ model: 'parent' }, () => reply());
  assert.throws(() => parent.modelCall({ model: 'parent-two' }, () => reply()), BudgetExceeded);
  assert.throws(() => parent.rollback(anchor));
});

test('async execution snapshots inputs and blocks concurrent/reentrant calls across runtimes', async () => {
  const store = new MemoryStore(); const runtime = new Runtime({ store }); const run = runtime.run('async');
  const other = new Runtime({ store }).run('other');
  let resolve; const wait = new Promise(r => { resolve = r; });
  const payload = { model: 'original' };
  const pending = run.modelCallAsync(payload, async args => { await wait; assert.equal(args.model, 'original'); return reply(); });
  payload.model = 'changed';
  await assert.rejects(other.modelCallAsync({}, async () => reply()), ConcurrentCallError);
  assert.throws(() => other.note({}), ConcurrentCallError); assert.throws(() => run.rollback(), ConcurrentCallError);
  resolve(); const complete = await pending; assert.equal(complete.payload.model, 'original');
  const reentrant = runtime.run('reentrant');
  reentrant.modelCall({}, () => { assert.throws(() => reentrant.modelCall({}, () => reply()), ConcurrentCallError); return reply(); });
});

test('async registered handlers, replay and confirmation execute through async methods', async () => {
  let calls = 0;
  const action = spec(async args => { calls++; return { text: args.text, usage: { input_tokens: 1, output_tokens: 1 } }; });
  const registry = new Registry([action]); const store = new MemoryStore();
  const run = new Runtime({ store, registry, policies: [{ decide: () => 'confirm' }] }).run('async-confirm');
  let token; await assert.rejects(run.toolCallAsync('echo', { text: 'x' }), error => { token = error.token; return error instanceof ConfirmationRequired; });
  const node = await run.confirmAsync(token); assert.equal(calls, 1);
  const replay = new Runtime({ store, registry, mode: 'replay' }).run('async-confirm');
  assert.equal((await replay.toolCallAsync('echo', { text: 'x' })).id, node.id); assert.equal(calls, 1);
  const sync = new Runtime({ registry }).run('sync-async-rejected');
  assert.throws(() => sync.toolCall('echo', { text: 'x' }), /async call method/); assert.equal(calls, 1);
});

test('unexpected promises remain locked until settled and cannot be redispatched', async () => {
  const runtime = new Runtime(); const run = runtime.run('unexpected-promise');
  let resolve; const pending = new Promise(r => { resolve = r; });
  assert.throws(() => run.modelCall({}, () => pending), /async call method/);
  assert.throws(() => run.modelCall({ different: true }, () => reply()), ConcurrentCallError);
  resolve(reply()); await new Promise(r => setImmediate(r));
  assert.throws(() => run.modelCall({}, () => reply()), DuplicateRecordingError);
});

test('read-only seven-method stores replay but cannot dispatch live without finalize', () => {
  const backing = new MemoryStore(); new Runtime({ store: backing }).run('capability').modelCall({}, () => reply());
  const store = { get: id => backing.get(id), put: n => backing.put(n), exists: id => backing.exists(id), children: id => backing.children(id), updateMeta: (id, patch) => backing.updateMeta(id, patch), walk: id => backing.walk(id), roots: () => backing.roots() };
  assert.equal(new Runtime({ store, mode: 'replay' }).run('capability').modelCall({}, () => { throw Error('never'); }).result.text, 'hello');
  assert.throws(() => new Runtime({ store }).run('live').modelCall({}, () => { throw Error('never'); }), /RecordingStore.finalize/);
});

test('ESM and CommonJS runtimes share dispatch locks in both import directions', () => {
  for (const [OuterRuntime, InnerRuntime, StoreClass] of [[Runtime, cjs.Runtime, cjs.MemoryStore], [cjs.Runtime, Runtime, MemoryStore]]) {
    const store = new StoreClass(); let nested; let calls = 0;
    const runtime = new OuterRuntime({ store, estimateTokens: () => {
      nested.modelCall({ model: 'same' }, () => { calls++; return reply(); });
      return 0;
    } });
    const outer = runtime.run('dual-lock'); nested = new InnerRuntime({ store }).run('dual-lock');
    assert.throws(() => outer.modelCall({ model: 'same' }, () => { calls++; return reply(); }), error => error.name === 'ConcurrentCallError');
    assert.equal(calls, 0);
    const node = nested.modelCall({ model: 'same' }, () => { calls++; return reply(); });
    assert.equal(calls, 1);
    assert.throws(() => outer.modelCall({ model: 'same' }, () => { calls++; return reply(); }), error => error.name === 'DuplicateRecordingError');
    assert.equal(calls, 1); assert.equal(store.get(node.id).meta.state, 'completed');
  }
});

test('a recording inserted by an estimator is refused before the handler executes', () => {
  const store = new MemoryStore(); let run; let calls = 0;
  const runtime = new Runtime({ store, estimateTokens: payload => {
    store.put(Node.make({ kind: 'model_call', parent: run.rootId, payload, result: reply() }));
    return 0;
  } });
  run = runtime.run('estimator-store-race');
  assert.throws(() => run.modelCall({ model: 'same' }, () => { calls++; return reply(); }), DuplicateRecordingError);
  assert.equal(calls, 0);
});

test('registered hybrid calls accept foreign-module Node cache hits in both directions', () => {
  for (const [ReaderRuntime, ReaderRegistry, ReaderSpec, WriterRuntime, WriterRegistry, WriterSpec, StoreClass] of [
    [Runtime, Registry, ActionSpec, cjs.Runtime, cjs.Registry, cjs.ActionSpec, cjs.MemoryStore],
    [cjs.Runtime, cjs.Registry, cjs.ActionSpec, Runtime, Registry, ActionSpec, MemoryStore],
  ]) {
    let calls = 0; const store = new StoreClass();
    const input = { name: 'echo', version: '1', description: 'cross-module', sideEffects: false, schema: { type: 'object' } };
    const writerRegistry = new WriterRegistry([new WriterSpec({ ...input, handler: () => { calls++; return reply(); } })]);
    const readerRegistry = new ReaderRegistry([new ReaderSpec({ ...input, handler: () => { calls++; throw Error('must never dispatch cached handler'); } })]);
    const node = new WriterRuntime({ store, registry: writerRegistry }).run('dual-tool').toolCall('echo', { text: 'x' });
    const hybrid = new ReaderRuntime({ store, registry: readerRegistry, mode: 'hybrid' }).run('dual-tool');
    assert.equal(hybrid.toolCall('echo', { text: 'x' }).id, node.id); assert.equal(calls, 1);
  }
});
