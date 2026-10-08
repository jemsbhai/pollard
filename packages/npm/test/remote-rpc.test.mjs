import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { RemoteStore, PostgresStore, KafkaStore } from '../dist/esm/remote.js';
import { Runtime } from '../dist/esm/runtime.js';
import { CostMeter, WindowMeter } from '../dist/esm/meters.js';
import { BudgetExceeded } from '../dist/esm/tree.js';
import { consumeStepResult, consumeStepResultAsync } from '../dist/esm/streaming.js';

test('worker RPC reports startup errors in ESM and CJS without leaking a live worker', () => {
  assert.throws(() => new RemoteStore({ backend: 'unsupported', storeId: 'test', create: false, timeoutMs: 5000 }), TypeError);
  const cjs = createRequire(import.meta.url)('../dist/cjs/remote.js');
  assert.throws(() => new cjs.RemoteStore({ backend: 'unsupported', storeId: 'test', create: false, timeoutMs: 5000 }), /unsupported remote backend/);
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', `import {RemoteStore} from './dist/esm/remote.js'; try { new RemoteStore({backend:'unsupported',storeId:'child',create:false,timeoutMs:5000}); } catch (e) { if (e.name!=='TypeError') process.exitCode=2; }`], { timeout: 10000, encoding: 'utf8' });
  assert.equal(child.error, undefined);
  assert.equal(child.status, 0, child.stderr);
});

test('remote facade validates options before launching a backend', () => {
  assert.throws(() => new PostgresStore('', {}), TypeError);
  assert.throws(() => new PostgresStore('postgresql://unused', { storeId: '' }), TypeError);
  assert.throws(() => new PostgresStore('postgresql://unused', { timeoutMs: NaN }), TypeError);
  assert.throws(() => new KafkaStore({ brokers: [], topic: 'topic' }), TypeError);
  assert.equal(typeof KafkaStore.prototype.pollardReserve, 'undefined');
});

test('prototype-shaped custom meter names cannot bypass a zero ceiling', () => {
  for (const name of ['constructor', '__proto__', 'toString']) {
    let dispatched = false;
    const meter = { name, precheckEstimate: () => 1, charge: () => 1 };
    const extra = JSON.parse(`{"${name}":0}`);
    const run = new Runtime({ meters: [meter] }).run(name, { budget: { extra } });
    assert.throws(() => run.modelCall({}, () => { dispatched = true; return {}; }), BudgetExceeded);
    assert.equal(dispatched, false);
  }
});

test('prototype-shaped meter charges and estimator fallback diagnostics remain own data', () => {
  for (const name of ['constructor', '__proto__', 'toString']) {
    const meter = { name, precheckIsEstimate: true, precheckEstimate: () => 1, charge: () => 0 };
    const run = new Runtime({ meters: [meter] }).run(name, { budget: { extra: JSON.parse(`{"${name}":1}`) } });
    const node = run.modelCall({}, () => ({}));
    assert.equal(node.meta.charges[name], 1);
    assert.equal(node.meta.accounting_fallbacks[name].source, 'precheck_estimate');
    assert.throws(() => run.modelCall({ next: true }, () => ({})), BudgetExceeded);
  }
});

test('unknown prototype-shaped model names have no configured price and custom windows count safely', () => {
  const meter = new CostMeter({ known: { inputPer1m: 1, outputPer1m: 2 } });
  for (const model of ['constructor', '__proto__', 'toString']) assert.equal(meter.charge('model_call', { model }, { usage: { input_tokens: 2, output_tokens: 1 } }), 0);
  const run = new Runtime({ meters: [new WindowMeter('__proto__', 1, 60)] }).run('window');
  run.modelCall({}, () => ({}));
  assert.throws(() => run.modelCall({ next: true }, () => ({})), BudgetExceeded);
});

test('stream merges keep prototype-shaped keys as data without changing Object.prototype', async () => {
  const chunks = [JSON.parse('{"delta":{"__proto__":{"pollard_test_pollution":true},"constructor":{"value":"data"}}}'), JSON.parse('{"delta":{"__proto__":{"next":1}}}')];
  try {
    for (const result of [consumeStepResult(chunks), await consumeStepResultAsync(chunks)]) {
      assert.equal(Object.prototype.pollard_test_pollution, undefined);
      assert.equal(Object.hasOwn(result, '__proto__'), true);
      assert.deepEqual(result.__proto__, { pollard_test_pollution: true, next: 1 });
      assert.deepEqual(result.constructor, { value: 'data' });
    }
  } finally { delete Object.prototype.pollard_test_pollution; delete Object.prototype.next; }
});
