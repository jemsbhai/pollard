import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalizeResponse, normalizeChatCompletion, normalizeMessage, normalizeConverse, ExactResultComparator, NormalizedModelComparator, ReplayContract } from '../dist/esm/index.js';

const fixture = JSON.parse(readFileSync(new URL('./python-parity.json', import.meta.url), 'utf8'));
test('provider outputs match the PyPI 1.6.0 Python oracle', () => {
  assert.equal(fixture.python_release, '1.6.0');
  const normalizers = { response: normalizeResponse, chat: normalizeChatCompletion, anthropic: normalizeMessage, bedrock: normalizeConverse };
  for (const example of fixture.providers) assert.deepEqual(normalizers[example.kind](example.input), example.expected, example.kind);
});
test('replay contracts and comparator differences match the Python oracle', () => {
  for (const example of fixture.comparisons) {
    assert.deepEqual(new ExactResultComparator().compare(example.recorded, example.live).toDict(), example.exact);
    assert.deepEqual(new NormalizedModelComparator().compare(example.recorded, example.live).toDict(), example.normalized);
  }
  const contract = new ReplayContract({ provider: 'mock', modelRevision: 'rev-1', applicationRevision: 'app-1', environment: { region: 'local' } });
  assert.deepEqual(contract.toDict(), fixture.contract.document);
  assert.deepEqual(contract.bind({ model: 'mock', prompt: 'hi' }), fixture.contract.bound);
});
