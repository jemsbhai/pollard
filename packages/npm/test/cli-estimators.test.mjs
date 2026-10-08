import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Runtime, SQLiteStore, OpenAITokenEstimator, fallbackEncodingName, EnergyMeter, StepMeter } from '../dist/esm/index.js';

function temporary(t) { const base = resolve(tmpdir()), dir = mkdtempSync(join(base, 'pollard-cli-')); t.after(() => { assert.equal(dirname(resolve(dir)), base); assert.ok(basename(dir).startsWith('pollard-cli-')); rmSync(dir, { recursive: true, force: true }); }); return dir; }
const cliPath = fileURLToPath(new URL('../dist/esm/cli.js', import.meta.url));
function cli(...args) { return spawnSync(process.execPath, [cliPath, ...args], { encoding: 'utf8' }); }

test('CLI reads recordings offline, produces escaped HTML, verifies and exports/imports seals', t => {
  const dir = temporary(t), path = join(dir, 'source.db'), html = join(dir, 'tree.html'), output = join(dir, 'subtree.json'), target = join(dir, 'target.db');
  const store = new SQLiteStore(path), run = new Runtime({ store }).run('<script>alert(1)</script>');
  run.modelCall({ model: 'mock', prompt: 'PRIVATE-PROMPT' }, () => ({ text: 'PRIVATE-OUTPUT', usage: { input_tokens: 2, output_tokens: 3 } })); store.close();
  assert.equal(cli('runs', path, '--json').status, 0);
  assert.equal(JSON.parse(cli('report', path, run.rootId).stdout).spent.tokens, 5);
  assert.equal(JSON.parse(cli('verify', path).stdout).ok, true);
  assert.equal(cli('show', path, run.rootId, '--html', html).status, 0);
  const content = readFileSync(html, 'utf8'); assert.ok(content.includes('&lt;script&gt;')); assert.ok(!content.includes('PRIVATE-PROMPT')); assert.ok(!content.includes('<script>alert'));
  assert.equal(cli('export', path, run.rootId, output).status, 0);
  assert.equal(cli('import', output, target).status, 0);
  assert.equal(JSON.parse(cli('seal', path, run.rootId).stdout).digest, JSON.parse(cli('seal', target, run.rootId).stdout).digest);
  assert.equal(cli('export', path, run.rootId, join(dir, 'subtree.jsonl'), '--format', 'jsonl').status, 0);
  assert.equal(cli('import', join(dir, 'subtree.jsonl'), join(dir, 'jsonl.db'), '--format', 'jsonl').status, 0);
});
test('CLI refuses missing read-only databases and requires explicit maintenance mode', t => {
  const dir = temporary(t), missing = join(dir, 'missing.db');
  assert.equal(cli('runs', missing).status, 1); assert.equal(existsSync(missing), false);
  assert.equal(cli('gc', missing).status, 1); assert.equal(existsSync(missing), false);
  assert.equal(cli('export', missing, 'bad', join(dir, 'bad'), '--format', 'unknown').status, 1);
  assert.equal(cli('--help').status, 0);
});
test('CLI diagnostics do not echo credential-bearing URLs or equal-form remote specifications', () => {
  const secret = 'DO-NOT-PRINT-THIS', url = `postgres://name:${secret}@localhost/database`;
  for (const args of [[url], ['runs', url], [`--unknown=${url}`], ['merge', `--into=pg-env:${secret}`, '--unknown']]) {
    const result = cli(...args); assert.equal(result.status, 1); assert.equal((result.stdout + result.stderr).includes(secret), false);
    assert.match(result.stderr, /remote store operation failed/);
  }
});
test('CLI rejects malformed encoded remote namespaces before opening a service', () => {
  for (const suffix of ['?prefix=%FF', '?prefix=%ED%A0%80', '?prefix=%ZZ', '?prefix=%00', '?prefix=first&prefix=second', '?unsupported=value', '#%FF']) {
    const result = cli('runs', `redis-env:POLLARD_UNSET_REVIEW_VARIABLE${suffix}`);
    assert.equal(result.status, 1); assert.equal(result.stdout, '');
    assert.match(result.stderr, /remote store operation failed/);
  }
});
test('token estimator follows Python leaf and modern-family fallback semantics', () => {
  let selected;
  const estimator = new OpenAITokenEstimator({ tokenizer: { encodingForModel() { throw new Error('unknown model'); }, getEncoding(name) { selected = name; return { encode: text => [...text] }; } } });
  assert.equal(estimator.estimateInputTokens({ model: 'gpt-5-test', messages: [{ role: 'user', content: 'hi😀' }], tools: [{ name: 'x' }] }), 11);
  assert.equal(selected, 'o200k_base'); assert.equal(fallbackEncodingName('deployment:gpt-4o-test'), 'o200k_base'); assert.equal(fallbackEncodingName('old-model'), 'cl100k_base');
  assert.throws(() => new OpenAITokenEstimator({ tokensPerMessage: -1 }));
  assert.ok(new OpenAITokenEstimator().estimateInputTokens({ model: 'gpt-4o', messages: [{ role: 'user', content: 'hello' }] }) > 3);
});
test('energy measurement integrates NVML counters around the real dispatch only', () => {
  let energy = 1000, reads = 0;
  const meter = new EnergyMeter({ nvml: { nvmlInit() {}, nvmlDeviceGetHandleByIndex: index => index, nvmlDeviceGetPowerUsage() { reads++; return 10_000; }, nvmlDeviceGetTotalEnergyConsumption: () => energy } });
  const runtime = new Runtime({ meters: [new StepMeter(), meter] }), run = runtime.run('energy');
  const node = run.modelCall({}, () => { energy += 2500; return { text: 'ok' }; });
  assert.equal(node.meta.charges.joules, 2.5); const before = reads;
  new Runtime({ store: runtime.store, mode: 'replay', meters: [meter] }).run('energy').modelCall({}, () => { throw new Error('no replay dispatch'); });
  assert.equal(reads, before);
});
