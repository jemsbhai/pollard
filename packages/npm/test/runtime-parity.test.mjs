import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  Runtime, MemoryStore, SQLiteStore, Node, BudgetExceeded, DuplicateRecordingError, ConcurrentCallError,
  StepMeter, TokenMeter, CostMeter, DepthMeter, WallClockMeter, WindowMeter, MeterPrecheckRefusal,
  ReplayContract, RevalidationComparison, ExactResultComparator, NormalizedModelComparator,
  markPostDispatchOutcomeUnknown, PostDispatchOutcomeUnknown, verify,
  ReservationLeaseLost, addCharges, priceTokens,
} from '../dist/esm/index.js';

const usage = (input = 2, output = 3) => ({ input_tokens: input, output_tokens: output });
const reply = () => ({ text: 'hello', usage: usage() });

test('sync streams aggregate strings, arrays and nested usage while retaining detached chunks', () => {
  const original = { delta: { text: 'hel', tool_calls: [{ name: 'one' }] } }; const deltas = [];
  const store = new MemoryStore(); const run = new Runtime({ store }).run('sync-stream');
  const node = run.modelCall({ model: 'm' }, function* () {
    yield original; original.delta.text = 'changed';
    yield { delta: { text: 'lo', tool_calls: [{ name: 'two' }], usage: { input_tokens: 2 } } };
    yield { usage: { output_tokens: 3 } };
  }, { keepChunks: true, onDelta: chunk => { assert.ok(Object.isFrozen(chunk)); deltas.push(chunk); } });
  assert.equal(node.result.text, 'hello'); assert.equal(node.result.tool_calls.length, 2);
  assert.deepEqual(node.result.usage, usage()); assert.equal(node.result.chunks[0].delta.text, 'hel');
  assert.equal(node.meta.charges.tokens, 5); assert.equal(deltas.length, 3);
  const before = [...store.walk(run.rootId)].map(n => n.toStorage()); const replayed = [];
  const replay = new Runtime({ store, mode: 'replay' }).run('sync-stream');
  assert.equal(replay.modelCall({ model: 'm' }, () => { throw Error('must not dispatch'); }, { onDelta: c => replayed.push(c) }).id, node.id);
  assert.deepEqual(replayed, deltas); assert.deepEqual([...store.walk(run.rootId)].map(n => n.toStorage()), before);
  assert.equal(replay.report().avoided.tokens, 5);
});

test('async streaming awaits callbacks, supports final result replacement, and replays chunks', async () => {
  const store = new MemoryStore(); const seen = [];
  const run = new Runtime({ store }).run('async-stream');
  const node = await run.modelCallAsync({}, async function* () { yield { delta: { text: 'draft' } }; yield { result: reply() }; }, { keepChunks: true, onDelta: async c => { await Promise.resolve(); seen.push(c); } });
  assert.equal(node.result.text, 'hello'); assert.equal(node.result.chunks.length, 2);
  const replay = new Runtime({ store, mode: 'replay' }).run('async-stream'); const replayed = [];
  await replay.modelCallAsync({}, () => { throw Error('not dispatched'); }, { onDelta: async c => { await Promise.resolve(); replayed.push(c); } });
  assert.deepEqual(replayed, seen);
});

test('stream failures and callback failures preserve the primary error, estimates and duplicate protection', async () => {
  for (const kind of ['iterator', 'callback']) {
    const error = Error('original failure'); const run = new Runtime({ estimateTokens: () => 7 }).run(`failed-${kind}`);
    await assert.rejects(run.modelCallAsync({}, async function* () { yield { delta: { text: 'partial' } }; if (kind === 'iterator') throw error; }, { onDelta: () => { if (kind === 'callback') throw error; } }), e => e === error);
    const failed = [...run.store.walk(run.rootId)].find(n => n.kind === 'model_call');
    assert.equal(failed.meta.state, 'failed'); assert.equal(failed.meta.charges.tokens, 7); assert.equal(failed.result, null);
    assert.throws(() => run.modelCall({}, reply), DuplicateRecordingError);
  }
  const original = Error('provider disconnect');
  const run = new Runtime().run('marked-error');
  assert.throws(() => run.modelCall({}, () => { throw new PostDispatchOutcomeUnknown(original); }), e => e === original);
  assert.equal(markPostDispatchOutcomeUnknown(original), original);
});

test('AbortSignal prevents dispatch before cancellation and holds the store while an aborted provider settles', async () => {
  const early = new AbortController(); early.abort(); const run = new Runtime().run('cancel'); let called = false;
  await assert.rejects(run.modelCallAsync({}, async () => { called = true; return reply(); }, { signal: early.signal }), { name: 'AbortError' });
  assert.equal(called, false); assert.equal([...run.store.walk(run.rootId)].length, 1);
  const controller = new AbortController(); let resolve; const provider = new Promise(r => { resolve = r; });
  const pending = run.modelCallAsync({}, () => provider, { signal: controller.signal, tokenEstimate: 8 });
  controller.abort(); await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(run.report().spent.tokens, 8);
  assert.throws(() => run.note({ other: true }), ConcurrentCallError);
  resolve(reply()); await new Promise(r => setImmediate(r));
  assert.throws(() => run.modelCall({}, reply), DuplicateRecordingError);
});

test('meters settle fractional cost, custom charges and wall time and enforce later overshoot', () => {
  const custom = { name: 'credits', precheckEstimate: () => 0.1, charge: () => 0.1 };
  const runtime = new Runtime({ meters: [new StepMeter(), new DepthMeter(), new WallClockMeter(), new TokenMeter(), new CostMeter({ m: { inputPer1m: 2, outputPer1m: 4 } }), custom] });
  const run = runtime.run('fractional', { budget: { usd: '0.000020', extra: { credits: 0.3 } } });
  const first = run.modelCall({ model: 'm' }, reply);
  assert.equal(first.meta.charges.usd, 0.000016); assert.equal(first.meta.charges.credits, 0.1); assert.ok(first.meta.charges.seconds >= 0);
  run.modelCall({ model: 'm', n: 2 }, reply);
  let called = false;
  assert.throws(() => run.modelCall({ model: 'm', n: 3 }, () => { called = true; return reply(); }), BudgetExceeded); assert.equal(called, false);
  const exact = new Runtime({ meters: [custom] }).run('decimal', { budget: { extra: { credits: 0.3 } } });
  for (let n = 0; n < 3; n++) exact.modelCall({ n }, reply);
  assert.equal(exact.report().spent.credits, 0.3);
  assert.throws(() => exact.modelCall({ n: 3 }, reply), BudgetExceeded);
});

test('configured estimates settle on missing usage and custom fallback hooks', () => {
  const meter = { name: 'credits', precheckIsEstimate: true, precheckEstimate: () => 0.25, charge: () => 0, precheckFallbackReason: () => 'price_unavailable' };
  const run = new Runtime({ meters: [new TokenMeter({ estimator: () => 4, reservedOutputTokens: 2 }), meter] }).run('fallback');
  const first = run.modelCall({}, () => ({ text: 'no usage' }));
  assert.equal(first.meta.charges.tokens, 6); assert.equal(first.meta.accounting_fallbacks.tokens.source, 'precheck_estimate');
  const second = run.modelCall({}, () => ({ usage: usage(0, 0) }));
  assert.equal(second.meta.charges.tokens, undefined); assert.equal(second.meta.charges.credits, 0.25);
  assert.equal(second.meta.accounting_fallbacks.credits.reason, 'price_unavailable');
  const optional = new Runtime({ meters: [new TokenMeter()] }).run('optional');
  assert.deepEqual(optional.modelCall({}, () => ({ text: 'missing' })).meta.charges, {});
});

test('meter governance refusals audit only caller-provided safe metadata and fail before dispatch', () => {
  const meter = { name: 'credits', precheckEstimate: () => { throw new MeterPrecheckRefusal('quota', 'credits unavailable', { requested: '1.5', remaining: 0, auditMeta: { diagnostic: 'quota_cache' } }); }, charge: () => 0 };
  const run = new Runtime({ meters: [meter] }).run('meter-refusal');
  assert.throws(() => run.modelCall({ secret: 'payload' }, () => { throw Error('must never dispatch'); }), BudgetExceeded);
  assert.equal(run.cursor.payload.reason, 'quota'); assert.equal(run.cursor.payload.requested, '1.5'); assert.equal(run.cursor.meta.diagnostic, 'quota_cache');
  assert.equal(JSON.stringify(run.cursor.toStorage()).includes('"secret"'), false);
  assert.throws(() => new MeterPrecheckRefusal('bad', undefined, { auditMeta: { charges: {} } }), /cannot override/);
  const programmingError = Error('configuration bug');
  const broken = new Runtime({ meters: [{ name: 'bad', charge: () => 0, precheckEstimate: () => { throw programmingError; } }] }).run('programming-error');
  assert.throws(() => broken.modelCall({}, reply), e => e === programmingError); assert.equal(broken.cursor.kind, 'root');
});

test('local and SQLite sliding windows apply to restarted handles without token estimates', () => {
  for (const store of [new MemoryStore(), new SQLiteStore(':memory:')]) {
    const runtime = new Runtime({ store, meters: [new WindowMeter('requests', 1, 60)] });
    runtime.run('window').modelCall({ n: 1 }, reply);
    const restarted = runtime.run('window');
    assert.throws(() => restarted.modelCall({ n: 2 }, reply), BudgetExceeded); assert.equal(restarted.cursor.payload.reason, 'window');
    store.close?.();
  }
});

test('SQLite runtime reservations settle actual charges and release abandoned pre-dispatch reservations', () => {
  const store = new SQLiteStore(':memory:');
  try {
    const runtime = new Runtime({ store, meters: [new TokenMeter({ estimator: () => 2 })] });
    const run = runtime.run('sqlite-budget', { budget: { tokens: 5 } });
    assert.equal(run.modelCall({ n: 1 }, reply).meta.charges.tokens, 5);
    assert.throws(() => runtime.run('sqlite-budget', { budget: { tokens: 5 } }).modelCall({ n: 2 }, reply), BudgetExceeded);
    assert.ok(verify(store, run.rootId).ok);
  } finally { store.close(); }
});

test('replay contracts are immutable and comparator differences are value-free JSON pointers', () => {
  const environment = { release: 'one' }; const contract = new ReplayContract({ provider: 'mock', modelRevision: 'v1', environment }); environment.release = 'two';
  const bound = contract.bind({ model: 'm' }); assert.equal(bound._pollard.replay_contract.environment.release, 'one');
  assert.throws(() => new ReplayContract({ provider: 'other' }).bind(bound), /different replay contract/);
  const normalized = new NormalizedModelComparator();
  assert.ok(normalized.compare({ text: 'same', usage: usage(), tool_calls: [{ id: 'first', function: { name: 't', arguments: '{"a":1,"b":2}' } }] }, { text: 'same', usage: usage(9), tool_calls: [{ id: 'second', function: { name: 't', arguments: '{"b":2,"a":1}' } }] }).matched);
  assert.deepEqual(new ExactResultComparator().compare({ 'secret/name': 'private' }, { 'secret/name': 'other-private' }).differencePaths, ['/secret~1name']);
  const many = new ExactResultComparator().compare(Object.fromEntries(Array.from({length: 102}, (_, i) => [String(i), 0])), {});
  assert.equal(many.differencePaths.length, 100); assert.ok(many.truncated); assert.throws(() => new RevalidationComparison({ matched: true, differencePaths: ['/x'] }));
});

test('explicit live revalidation records a separate charged observation and resumes recorded lineage', async () => {
  const contract = new ReplayContract({ provider: 'mock', applicationRevision: 'v2' }); const store = new MemoryStore();
  const runtime = new Runtime({ store }); const recorded = runtime.run('revalidate'); const payload = { model: 'm' };
  const baseline = recorded.modelCall(payload, reply); const oldText = baseline.resultText;
  const next = recorded.modelCall({ model: 'next' }, reply);
  const run = runtime.run('revalidate');
  const report = await run.revalidateModelCallAsync(payload, async passed => { assert.deepEqual(passed, payload); return { text: 'hello', usage: usage(9) }; }, { contract, observationId: 'observation' });
  assert.ok(report.matched); assert.equal(report.exactMatch, false); assert.equal(report.recordedNodeId, baseline.id); assert.equal(run.cursorId, baseline.id);
  assert.equal(store.get(baseline.id).resultText, oldText); assert.equal(store.get(report.evidenceNodeId).payload.event, 'model_revalidation');
  assert.equal(store.get(report.liveNodeId).meta.charges.tokens, 12); assert.equal(report.charges.tokens, 12);
  const hybrid = new Runtime({ store, mode: 'hybrid' }).run('revalidate'); hybrid.modelCall(payload, reply); assert.equal(hybrid.modelCall({ model: 'next' }, reply).id, next.id);
  assert.throws(() => runtime.run('revalidate').revalidateModelCall(payload, reply, { contract, observationId: 'observation' }), DuplicateRecordingError);
  assert.throws(() => new Runtime({ store, mode: 'replay' }).run('revalidate').revalidateModelCall(payload, reply, { contract }), /record mode/);
});

test('revalidation comparator failures preserve the live result and safe failure evidence', () => {
  const runtime = new Runtime(); const run = runtime.run('comparison-failed'); const baseline = run.modelCall({}, reply); run.rollback();
  const error = Error('secret provider content');
  assert.throws(() => run.revalidateModelCall({}, reply, { contract: new ReplayContract({ provider: 'mock' }), comparator: { name: 'broken', compare: () => { throw error; } } }), e => e === error);
  assert.equal(run.cursorId, baseline.id);
  const evidence = [...run.store.walk(run.rootId)].find(n => n.payload.event === 'model_revalidation_comparison_failed');
  assert.ok(evidence); assert.equal(evidence.payload.error_type, 'Error'); assert.equal(JSON.stringify(evidence.toStorage()).includes(error.message), false);
});

test('resume selects deepest unpruned leaf and observers see persisted completion without replacing outcomes', () => {
  const observed = []; const runtime = new Runtime({ onNode: node => { observed.push(node); throw Error('observer failed'); } });
  const run = runtime.run('resume'); const first = run.modelCall({}, reply); run.modelCall({ next: true }, reply);
  assert.equal(runtime.resume('resume').cursorId, run.cursorId); run.prune(); assert.equal(runtime.resume('resume').cursorId, first.id);
  assert.equal(observed.length, 3); assert.equal(observed[1].meta.state, 'completed');
});

test('measurement lifecycle starts before dispatch, stops before charging and retains readings', () => {
  const order = [];
  const meter = { name: 'joules', precheckEstimate: () => null, measure: () => ({ start: () => order.push('start'), stop: () => order.push('stop'), readings: () => ({ energy_j: 1.5 }) }), charge: (_kind, _payload, _result, meta) => { order.push('charge'); return meta.energy_j; } };
  const run = new Runtime({ meters: [meter] }).run('measure');
  const node = run.modelCall({}, () => { order.push('dispatch'); return reply(); });
  assert.deepEqual(order, ['start', 'dispatch', 'stop', 'charge']); assert.equal(node.meta.charges.joules, 1.5); assert.equal(node.meta.energy_j, 1.5);
});

test('measurement cleanup and store failures do not replace the primary provider exception', () => {
  const primary = Error('provider failed'), cleanup = Error('measurement stop failed');
  const meter = { name: 'joules', precheckEstimate: () => 3, charge: () => 0, measure: () => ({ start() {}, stop() { throw cleanup; }, readings: () => ({ energy_j: 2 }) }) };
  const run = new Runtime({ meters: [meter] }).run('measurement-failed');
  assert.throws(() => run.modelCall({}, () => { throw primary; }), error => error === primary && error.cause === cleanup);
  const failed = [...run.store.walk(run.rootId)].find(n => n.kind === 'model_call');
  assert.equal(failed.meta.energy_j, 2); assert.equal(failed.meta.charges.joules, 3);
  const store = new MemoryStore(); const originalUpdate = store.updateMeta.bind(store);
  store.updateMeta = (id, patch) => { if (patch.state === 'failed') throw cleanup; originalUpdate(id, patch); };
  const another = new Runtime({ store }).run('cleanup-store');
  assert.throws(() => another.modelCall({}, () => { throw primary; }), error => error === primary);
  assert.throws(() => another.modelCall({}, reply), DuplicateRecordingError);
});

test('shared SQLite handles reserve concurrent calls without double counting pending charges', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'pollard-reservations-')); const filename = join(directory, 'shared.db');
  const firstStore = new SQLiteStore(filename), secondStore = new SQLiteStore(filename);
  try {
    const first = new Runtime({ store: firstStore, meters: [new StepMeter()] }).run('shared', { budget: { steps: 2 } });
    let resolve; const pending = first.modelCallAsync({ n: 1 }, () => new Promise(r => { resolve = r; }));
    const second = new Runtime({ store: secondStore, meters: [new StepMeter()] }).run('shared', { budget: { steps: 2 } });
    assert.equal(second.modelCall({ n: 2 }, reply).meta.charges.steps, 1);
    resolve(reply()); await pending;
    assert.equal(first.report().spent.steps, 2);
    assert.throws(() => second.modelCall({ n: 3 }, reply), BudgetExceeded);
  } finally { firstStore.close(); secondStore.close(); rmSync(directory, { recursive: true, force: true }); }
});

test('depth-only budgets do not create an empty renewable reservation', () => {
  const store = new SQLiteStore(':memory:');
  try {
    const node = new Runtime({ store }).run('depth-only', { budget: { depth: 1 } }).modelCall({}, reply);
    assert.equal(node.meta.reservation_id, undefined); assert.equal(node.meta.reservation_lease, undefined);
  } finally { store.close(); }
});

test('settlement uncertainty and lease loss preserve completed results and throw the original failure', () => {
  const error = Error('ledger unavailable'); let settlements = 0;
  const store = new MemoryStore(); store.pollardReserve = () => ({ ok: true }); store.pollardRenew = () => true; store.pollardRelease = () => {};
  store.pollardSettle = () => { settlements++; throw error; };
  const run = new Runtime({ store }).run('uncertain', { budget: { steps: 3 } });
  assert.throws(() => run.modelCall({}, reply), e => e === error);
  assert.equal(settlements, 1); assert.equal(run.cursor.result.text, 'hello'); assert.equal(run.cursor.meta.state, 'completed'); assert.equal(run.cursor.meta.settlement.status, 'uncertain');
  const lost = new MemoryStore(); lost.pollardReserve = () => ({ ok: true }); lost.pollardRenew = () => false; lost.pollardRelease = () => {}; lost.pollardSettle = () => {};
  const lease = new Runtime({ store: lost }).run('lease-lost', { budget: { steps: 3 } });
  assert.throws(() => lease.modelCall({}, reply), ReservationLeaseLost); assert.equal(lease.cursor.result.text, 'hello'); assert.equal(lease.cursor.meta.reservation_lease.status, 'lost');
});

test('decimal charge arithmetic preserves subnormals and Python window ledger identity', () => {
  assert.equal(addCharges(Number.MIN_VALUE, Number.MIN_VALUE), 1e-323); assert.equal(addCharges(1, Number.MIN_VALUE), 1);
  assert.equal(priceTokens(3, 0.1), 3e-7);
  assert.equal(new WindowMeter('requests', 1, 60).ledgerKey('root'), '01e55b45d94ba625694426d6b504664135e1a6a4e19bd37c5a47ecab1940fbb3');
});
