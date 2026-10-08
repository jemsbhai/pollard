import test from 'node:test';
import assert from 'node:assert/strict';
import { makeResponsesFn, makeChatCompletionsFn, makeMessagesFn, makeConverseFn, makeCompletionFn, normalizeResponse, normalizeMessage, normalizeConverse, normalizeChatCompletion, OpenAIResponseError, AnthropicStreamError, BedrockStreamError } from '../dist/esm/adapters.js';
import { isPostDispatchOutcomeUnknown } from '../dist/esm/streaming.js';

async function collect(stream) { const chunks = []; for await (const chunk of stream) chunks.push(chunk); return chunks; }
async function* events(...items) { yield* items; }

test('provider normalizers preserve raw usage and include cache charges exactly once', () => {
  const response = normalizeResponse({ output: [{ type: 'message', content: [{ type: 'output_text', text: 'hello' }] }, { type: 'function_call', call_id: 't', name: 'find', arguments: '{}' }], usage: { input_tokens: 2, output_tokens: 3 } });
  assert.equal(response.text, 'hello'); assert.deepEqual(response.tool_calls, [{ call_id: 't', name: 'find', arguments: '{}' }]); assert.deepEqual(response.provider_usage, response.usage);
  assert.deepEqual(normalizeMessage({ usage: { input_tokens: 2, output_tokens: 3, cache_creation_input_tokens: 5, cache_read_input_tokens: 7 }, content: [{ type: 'tool_use', id: 't', name: 'find', input: {} }] }).usage, { input_tokens: 14, output_tokens: 3 });
  const bedrock = normalizeConverse({ usage: { inputTokens: 2, outputTokens: 3, cacheReadInputTokens: 5, cacheWriteInputTokens: 7 }, output: { message: { content: [{ text: 'hi' }, { toolUse: { toolUseId: 't', name: 'find', input: {} } }] } } });
  assert.deepEqual(bedrock.usage, { input_tokens: 14, output_tokens: 3 }); assert.equal(bedrock.text, 'hi'); assert.equal(bedrock.tool_calls[0].name, 'find');
  assert.deepEqual(normalizeChatCompletion({ usage: { prompt_tokens: 3, completion_tokens: 4 }, choices: [{ message: { content: 'ok' } }] }).usage, { input_tokens: 3, output_tokens: 4 });
});
test('invalid usage is preserved diagnostically and never normalized to valid zero', () => {
  for (const input of [-1, 0.5, true, null, '3']) {
    const result = normalizeChatCompletion({ usage: { prompt_tokens: input, completion_tokens: 4 } });
    assert.equal(result.usage, undefined); assert.equal(result.provider_usage.prompt_tokens, input);
  }
  assert.throws(() => normalizeChatCompletion({ usage: { prompt_tokens: Number.MAX_SAFE_INTEGER + 1, completion_tokens: 4 } }), TypeError);
  assert.equal(normalizeMessage({ usage: { input_tokens: 1, output_tokens: 2, cache_read_input_tokens: -1 } }).usage, undefined);
  assert.equal(normalizeConverse({ usage: { inputTokens: 1, outputTokens: 2, cacheReadInputTokens: -1 } }).usage, undefined);
  assert.equal(normalizeResponse({}).usage, undefined);
});
test('adapters snapshot defaults, remove _pollard, and preserve caller errors', async () => {
  let sent; const defaults = { model: 'mock', nested: { keep: true } };
  const call = makeResponsesFn({ responses: { create: async params => { sent = params; return { output_text: 'done' }; } } }, { defaults });
  defaults.nested.keep = false;
  assert.equal((await call({ model: 'override', _pollard: { secret: true } })).text, 'done');
  assert.equal(sent.model, 'override'); assert.equal(sent._pollard, undefined); assert.equal(sent.nested.keep, true);
  const failure = new Error('network');
  await assert.rejects(makeResponsesFn({ responses: { create: () => { throw failure; } } })({}), error => error === failure && isPostDispatchOutcomeUnknown(error));
  assert.equal((await makeCompletionFn(async () => ({ choices: [{ message: { content: 'proxy' } }] }))({})).text, 'proxy');
});
test('Responses streams retain deltas, terminal results and reject truncation/errors', async () => {
  const call = makeResponsesFn({ responses: { create: async () => events({ type: 'response.output_text.delta', delta: 'hi' }, { type: 'response.completed', response: { output_text: 'hi', usage: { input_tokens: 2, output_tokens: 1 } } }) } }, { stream: true });
  const chunks = await collect(await call({})); assert.deepEqual(chunks[0].delta, { text: 'hi' }); assert.equal(chunks[1].result.text, 'hi');
  for (const event of [{ type: 'response.output_text.delta', delta: 'partial' }, { type: 'response.failed', response: { error: { message: 'bad' } } }]) {
    await assert.rejects(collect(await makeResponsesFn({ responses: { create: () => events(event) } }, { stream: true })({})), error => error instanceof OpenAIResponseError && isPostDispatchOutcomeUnknown(error));
  }
});
test('Chat streams join tool fragments, retain final usage, and require finish_reason', async () => {
  const stream = events(
    { id: 'a', model: 'mock', choices: [{ delta: { content: 'Hi', tool_calls: [{ index: 0, id: 't', type: 'function', function: { name: 'fi', arguments: '{' } }] } }] },
    { choices: [{ delta: { tool_calls: [{ index: 0, function: { name: 'nd', arguments: '}' } }] }, finish_reason: 'tool_calls' }] },
    { choices: [], usage: { prompt_tokens: 2, completion_tokens: 3 } });
  let params; const call = makeChatCompletionsFn({ chat: { completions: { create: value => { params = value; return stream; } } } }, { stream: true });
  const result = (await collect(await call({}))).at(-1).result;
  assert.equal(params.stream_options.include_usage, true); assert.equal(result.text, 'Hi'); assert.deepEqual(result.usage, { input_tokens: 2, output_tokens: 3 }); assert.equal(result.tool_calls[0].function.name, 'find'); assert.equal(result.tool_calls[0].function.arguments, '{}');
  await assert.rejects(collect(await makeChatCompletionsFn({ chat: { completions: { create: () => events({ choices: [] }) } } }, { stream: true })({})), OpenAIResponseError);
});
test('Anthropic streams account cache tokens, collect tools and fail closed on truncation', async () => {
  const client = { messages: { create: () => events(
    { type: 'message_start', message: { id: 'm', model: 'mock', usage: { input_tokens: 2, cache_read_input_tokens: 5 } } },
    { type: 'content_block_start', index: 0, content_block: { type: 'tool_use', id: 't', name: 'find' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'input_json_delta', partial_json: '{"a":1}' } },
    { type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: 'ok' } },
    { type: 'message_delta', delta: { stop_reason: 'tool_use' }, usage: { output_tokens: 3 } },
    { type: 'message_stop' }) } };
  const result = (await collect(await makeMessagesFn(client, { stream: true })({}))).at(-1).result;
  assert.equal(result.text, 'ok'); assert.deepEqual(result.usage, { input_tokens: 7, output_tokens: 3 }); assert.deepEqual(result.tool_calls[0].input, { a: 1 });
  await assert.rejects(collect(await makeMessagesFn({ messages: { create: () => events({ type: 'message_start' }) } }, { stream: true })({})), AnthropicStreamError);
});
test('Bedrock streams account usage, preserve malformed tool input and require messageStop', async () => {
  const client = { converseStream: () => ({ stream: events(
    { contentBlockStart: { contentBlockIndex: 0, start: { toolUse: { toolUseId: 't', name: 'find' } } } },
    { contentBlockDelta: { contentBlockIndex: 0, delta: { toolUse: { input: '{broken' } } } },
    { contentBlockDelta: { contentBlockIndex: 1, delta: { text: 'ok' } } },
    { messageStop: { stopReason: 'tool_use' } },
    { metadata: { usage: { inputTokens: 2, outputTokens: 3, cacheReadInputTokens: 5 } } }) }) };
  const result = (await collect(await makeConverseFn(client, { stream: true })({}))).at(-1).result;
  assert.equal(result.text, 'ok'); assert.equal(result.tool_calls[0].input, '{broken'); assert.deepEqual(result.usage, { input_tokens: 7, output_tokens: 3 });
  await assert.rejects(collect(await makeConverseFn({ converseStream: () => ({ stream: events({ throttlingException: { message: 'busy' } }) }) }, { stream: true })({})), BedrockStreamError);
});
test('provider token counts are explicit requests and filter generation-only fields', async () => {
  let counted; const call = makeMessagesFn({ messages: { create: () => ({}), countTokens: params => { counted = params; return { input_tokens: 7 }; } } });
  assert.equal(await call.estimateInputTokens({ model: 'mock', messages: [], max_tokens: 99 }), 7); assert.equal(counted.max_tokens, undefined);
  const converse = makeConverseFn({ countTokens: params => { counted = params; return { inputTokens: 6 }; } }, { countTokens: true });
  assert.equal(await converse.estimateInputTokens({ modelId: 'mock', messages: [], inferenceConfig: { maxTokens: 30 } }), 6); assert.deepEqual(counted, { modelId: 'mock', input: { converse: { messages: [] } } });
  assert.equal(await makeConverseFn({}).estimateInputTokens({}), null);
});

test('malformed SDK serializers cannot turn a provider result into an empty success', async () => {
  for (const value of [null, false, 0, 'invalid', []]) {
    const response = { toJSON: () => value };
    assert.throws(() => normalizeResponse(response), /serializer must return an object/);
    await assert.rejects(makeResponsesFn({ responses: { create: () => response } })({}), error => error instanceof TypeError && isPostDispatchOutcomeUnknown(error));
  }
  assert.equal(normalizeResponse({ toJSON: () => null, model_dump: () => ({ output_text: 'valid alternate' }) }).text, 'valid alternate');
});

test('Responses terminal fallback is only used when the response is null or absent', async () => {
  for (const response of [false, 0, '', []]) {
    const call = makeResponsesFn({ responses: { create: () => events({ type: 'response.completed', response }) } }, { stream: true });
    await assert.rejects(collect(await call({})), error => error instanceof TypeError && isPostDispatchOutcomeUnknown(error));
  }
  const call = makeResponsesFn({ responses: { create: () => events({ type: 'response.output_text.delta', delta: 'retained' }, { type: 'response.completed', response: null }) } }, { stream: true });
  assert.equal((await collect(await call({}))).at(-1).result.text, 'retained');
});
