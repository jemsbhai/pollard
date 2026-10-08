import test from 'node:test';
import assert from 'node:assert/strict';
import { Runtime, MemoryStore, BudgetExceeded } from '../dist/esm/index.js';
import { TokenmasterMeter, TokenmasterCostMeter, tokenmasterGovernanceMeters, exclusiveTurnUsage } from '../dist/esm/tokenmaster.js';

function client(overrides = {}) {
  return {
    getProfile: model => ({ model_id: model }),
    createMeter: profile => ({ profile, record: turn => ({ ...turn }), state: () => ({ pressure: 0.5 }), advise: () => ({ status: 'continue' }) }),
    checkRequestLimits(target, options) {
      const capacity = options.capacity === 'effective' ? 80 : 100;
      const output = Math.max(options.requestedOutputTokens ?? 0, options.reservedOutputTokens), context = options.inputTokens + output;
      return { allowed: options.inputTokens <= 90 && context <= capacity && output <= 20, model_id: typeof target === 'string' ? target : target.model_id, context_output_tokens: output, violations: context > capacity ? ['context'] : [], input_exceeded: options.inputTokens > 90, context_exceeded: context > capacity, output_exceeded: output > 20, input_tokens: options.inputTokens, max_input_tokens: 90, context_tokens: context, capacity, requested_output_tokens: output, max_output_tokens: 20 };
    },
    quoteEstimate(target, options) {
      assert.equal(options.conservative, true);
      return { model_id: typeof target === 'string' ? target : target.model_id, currency: 'USD', input_tokens: options.inputTokens, reserved_output_tokens: options.reservedOutputTokens, input_rate: 8, output_rate: 16 };
    },
    quoteUsage(target, turn) {
      const long = turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens > 50;
      return { model_id: typeof target === 'string' ? target : target.model_id, currency: 'USD', pricing: { input: long ? 8 : 2, cache_read: 1, cache_write: 3, output: long ? 16 : 4 } };
    }, ...overrides,
  };
}
const usage = (input_tokens = 10, output_tokens = 5) => ({ input_tokens, output_tokens });

test('exclusive categories avoid double counting OpenAI nested cache/reasoning usage', () => {
  assert.deepEqual(exclusiveTurnUsage({ usage: usage(100, 30), provider_usage: { prompt_tokens: 100, completion_tokens: 30, prompt_tokens_details: { cached_tokens: 40 }, completion_tokens_details: { reasoning_tokens: 10 } } }), { input_tokens: 60, cache_read_tokens: 40, cache_write_tokens: 0, output_tokens: 20, reasoning_tokens: 10 });
  assert.deepEqual(exclusiveTurnUsage({ usage: usage(15, 5), provider_usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 3, cache_creation_input_tokens: 2 } }), { input_tokens: 10, cache_read_tokens: 3, cache_write_tokens: 2, output_tokens: 5, reasoning_tokens: 0 });
  assert.deepEqual(exclusiveTurnUsage({ usage: usage(15, 5), provider_usage: { inputTokens: 10, outputTokens: 5, cacheReadInputTokens: 3, cacheWriteInputTokens: 2 } }), { input_tokens: 10, cache_read_tokens: 3, cache_write_tokens: 2, output_tokens: 5, reasoning_tokens: 0 });
});

test('tokenmaster meters settle usage, retain state/advice and support explicit governance factory', () => {
  const run = new Runtime({ meters: tokenmasterGovernanceMeters({ client: client(), model: 'profile', estimator: () => 10, reservedOutput: 5, expectedRemainingTurns: 2 }) }).run('tokenmaster');
  const node = run.modelCall({ model: 'other' }, () => ({ usage: usage() }));
  assert.equal(node.meta.charges.tokens, 15); assert.equal(node.meta.charges.usd, 0.00004); assert.equal(node.meta.charges.steps, 1); assert.ok(node.meta.charges.seconds >= 0);
  assert.equal(node.meta.tokenmaster.turn.model_id, 'profile'); assert.equal(node.meta.tokenmaster.state.pressure, 0.5); assert.equal(node.meta.tokenmaster.advice.status, 'continue'); assert.equal(node.meta.tokenmaster.task.expected_remaining_turns, 2);
});

test('profile enforcement is opt-in, checks requested output aliases and effective capacity', () => {
  const legacy = new TokenmasterMeter({ client: client(), estimator: () => 10, reservedOutput: 3 });
  assert.equal(legacy.precheckEstimate('model_call', { max_tokens: 'not parsed without enforcement' }), 13);
  assert.throws(() => new TokenmasterMeter({ client: client(), enforceProfileLimits: true }), /requires an estimator/);
  for (const field of ['max_tokens','max_completion_tokens','max_output_tokens']) {
    const meter = new TokenmasterMeter({ client: client(), estimator: () => 85, enforceProfileLimits: true });
    const run = new Runtime({ meters: [meter] }).run(field); let called = false;
    assert.throws(() => run.modelCall({ model: 'm', [field]: 20 }, () => { called = true; return { usage: usage() }; }), BudgetExceeded);
    assert.equal(called, false); assert.equal(run.cursor.payload.reason, 'tokenmaster_profile_limit'); assert.equal(run.cursor.payload.requested, '105'); assert.equal(run.cursor.meta.tokenmaster.limits.context_exceeded, true);
  }
  const effective = new Runtime({ meters: [new TokenmasterMeter({ client: client(), estimator: () => 81, enforceProfileLimits: true, profileCapacity: 'effective' })] }).run('effective');
  assert.throws(() => effective.modelCall({ model: 'm' }, () => ({ usage: usage() })), BudgetExceeded);
});

test('profile unavailable and missing estimates are auditable before dispatch', () => {
  for (const options of [{ estimator: () => null }, { estimator: () => 10, client: client({ checkRequestLimits: () => { throw Error('sensitive profile service error'); } }) }]) {
    const run = new Runtime({ meters: [new TokenmasterMeter({ client: client(), enforceProfileLimits: true, ...options })] }).run('profile-unavailable');
    assert.throws(() => run.modelCall({ model: 'm' }, () => { throw Error('never'); }), BudgetExceeded);
    assert.equal(run.cursor.payload.reason, 'tokenmaster_profile_unavailable'); assert.equal(JSON.stringify(run.cursor.toStorage()).includes('sensitive'), false);
  }
});

test('model binding follows each result unless explicitly fixed and diagnostics never replace completed calls', () => {
  const models = [];
  const supplied = client({ getProfile: model => { models.push(model); return { model_id: model }; } });
  const run = new Runtime({ meters: [new TokenmasterMeter({ client: supplied })] }).run('models');
  run.modelCall({ model: 'request' }, () => ({ model: 'result-one', usage: usage() })); run.modelCall({ model: 'request' }, () => ({ model: 'result-two', usage: usage() }));
  assert.deepEqual(models, ['result-one', 'result-two']);
  const broken = new Runtime({ meters: [new TokenmasterMeter({ client: client({ createMeter: () => { throw Error('diagnostic failure'); } }) })] }).run('diagnostic-failed');
  const node = broken.modelCall({ model: 'm' }, () => ({ text: 'completed', usage: usage() }));
  assert.equal(node.result.text, 'completed'); assert.equal(node.meta.charges.tokens, 15); assert.equal(node.meta.tokenmaster.meter.status, 'error');
});

test('cost preflight requests conservative tier quote and enforces fractional USD budget', () => {
  let request;
  const supplied = client({ quoteEstimate: (target, options) => { request = options; return client().quoteEstimate(target, options); } });
  const meter = new TokenmasterCostMeter({ client: supplied, estimator: () => 10, reservedOutput: 5 });
  assert.equal(meter.precheckEstimate('model_call', { model: 'm', max_tokens: 20 }), 0.0004); assert.equal(request.reservedOutputTokens, 20); assert.equal(request.conservative, true);
  const run = new Runtime({ meters: [meter] }).run('cost-refusal', { budget: { usd: '0.0001' } });
  assert.throws(() => run.modelCall({ model: 'm', max_tokens: 20 }, () => { throw Error('never'); }), BudgetExceeded); assert.equal(run.cursor.payload.estimated, 'true');
});

test('missing/incomplete/non-USD preflight pricing refuses and failed postflight pricing settles estimate', async () => {
  for (const supplied of [client({ quoteEstimate: () => { throw Error('missing prices'); } }), client({ quoteEstimate: (target, options) => ({ ...client().quoteEstimate(target, options), currency: 'EUR' }) }), client({ quoteEstimate: (target, options) => ({ ...client().quoteEstimate(target, options), output_rate: null }) })]) {
    const run = new Runtime({ meters: [new TokenmasterCostMeter({ client: supplied, estimator: () => 10 })] }).run('pricing-refusal');
    assert.throws(() => run.modelCall({ model: 'm' }, () => { throw Error('never'); }), BudgetExceeded);
  }
  const store = new MemoryStore(); const supplied = client({ quoteUsage: () => { throw Error('billing unavailable'); } });
  const runtime = new Runtime({ store, meters: [new TokenmasterCostMeter({ client: supplied, estimator: () => 10, reservedOutput: 5 })] });
  const run = runtime.run('postflight'); const node = await run.modelCallAsync({ model: 'm' }, async () => ({ text: 'completed', usage: usage() }));
  assert.equal(node.meta.charges.usd, 0.00016); assert.equal(node.meta.tokenmaster.cost.status, 'unavailable'); assert.equal(node.meta.accounting_fallbacks.usd.reason, 'exact_pricing_unavailable');
  const replay = new Runtime({ store, mode: 'replay', meters: runtime.meters }).run('postflight');
  assert.equal(replay.modelCall({ model: 'm' }, () => { throw Error('never'); }).id, node.id);
});

test('cost settlement prices exclusive categories and tokenmeter records postflight profile overages', () => {
  const runtime = new Runtime({ meters: tokenmasterGovernanceMeters({ client: client(), estimator: () => 5, enforceProfileLimits: true }) });
  const run = runtime.run('exclusive-cost');
  const node = run.modelCall({ model: 'm' }, () => ({ usage: usage(100, 30), provider_usage: { input_tokens: 100, output_tokens: 30, input_tokens_details: { cached_tokens: 40 }, output_tokens_details: { reasoning_tokens: 10 } } }));
  assert.equal(node.meta.charges.tokens, 130); assert.equal(node.meta.charges.usd, 0.001);
  assert.equal(node.meta.tokenmaster.limits.allowed, false); assert.equal(node.meta.tokenmaster.limits.phase, 'settlement'); assert.equal(node.result.usage.input_tokens, 100);
});
