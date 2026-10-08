/** Caller-owned SDK adapters. Importing this module never imports an SDK or reads credentials. */
import { IdentityPayload, JsonObject, snapshot } from './identity.js';
import { markPostDispatchOutcomeUnknown } from './streaming.js';

type ObjectMap = Record<string, any>;
export interface AdapterOptions { stream?: boolean; defaults?: JsonObject; }
export type ProviderStep = (payload: IdentityPayload) => Promise<JsonObject | AsyncIterable<JsonObject>>;
export interface EstimatingProviderStep extends ProviderStep { estimateInputTokens(payload: IdentityPayload): Promise<number | null>; }

function unknownOutcome(error: unknown): unknown {
  return markPostDispatchOutcomeUnknown(error);
}
function object(value: unknown): ObjectMap { return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as ObjectMap : {}; }
function asObject(value: unknown): JsonObject {
  const data = object(value);
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('provider response must be an object');
  let attemptedConversion = false;
  for (const method of ['toJSON', 'to_dict', 'model_dump']) if (typeof data[method] === 'function') {
    attemptedConversion = true;
    const converted: unknown = data[method]();
    if (converted !== null && typeof converted === 'object' && !Array.isArray(converted)) return snapshot(converted as JsonObject);
  }
  if (attemptedConversion) throw new TypeError('provider response serializer must return an object');
  return snapshot(data as JsonObject);
}
function request(defaults: JsonObject, payload: IdentityPayload): ObjectMap { const params = snapshot({ ...defaults, ...payload }); delete params._pollard; return params; }
function valid(value: unknown): value is number { return Number.isSafeInteger(value) && (value as number) >= 0; }
function integer(value: unknown, ...names: string[]): number { const data = object(value); for (const name of names) if (valid(data[name])) return data[name]; return 0; }
function normalizedUsage(result: ObjectMap, names: string[][], extras: string[] = []): void {
  const raw = result.usage;
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) { delete result.usage; return; }
  result.provider_usage = snapshot(raw);
  if (!names.every(group => group.some(name => valid(raw[name]))) || !extras.every(name => !(name in raw) || valid(raw[name]))) { delete result.usage; return; }
  const input = integer(raw, ...names[0]) + extras.reduce((sum, key) => sum + integer(raw, key), 0);
  if (!Number.isSafeInteger(input)) { delete result.usage; return; }
  result.usage = { input_tokens: input, output_tokens: integer(raw, ...names[1]) };
}
const openaiFields = [['input_tokens', 'prompt_tokens'], ['output_tokens', 'completion_tokens']];
export class OpenAIResponseError extends Error {
  override name = 'OpenAIResponseError';
  readonly rawEvent: JsonObject; readonly eventName: string; readonly responseId: string | null; readonly code: string | null;
  constructor(event: JsonObject) {
    const response = object(event.response), detail = object(response.error ?? event.error);
    super(typeof detail.message === 'string' ? detail.message : 'OpenAI stream ended without a terminal event');
    this.rawEvent = snapshot(event); this.eventName = String(event.type ?? 'response.failed'); this.responseId = typeof response.id === 'string' ? response.id : null; this.code = typeof detail.code === 'string' ? detail.code : null;
  }
}
export class AnthropicStreamError extends Error {
  override name = 'AnthropicStreamError'; readonly rawEvent: JsonObject;
  constructor(event: JsonObject) { super(String(object(event.error).message ?? 'Anthropic stream ended without message_stop')); this.rawEvent = snapshot(event); }
}
export class BedrockStreamError extends Error {
  override name = 'BedrockStreamError'; readonly rawEvent: JsonObject;
  constructor(public readonly eventName: string, event: JsonObject) { super(`Bedrock stream ${eventName}: ${object(event[eventName]).message ?? 'stream did not complete'}`); this.rawEvent = snapshot(event); }
}

export function normalizeChatCompletion(value: unknown): JsonObject {
  const result: ObjectMap = asObject(value); normalizedUsage(result, openaiFields);
  const message = object(object(Array.isArray(result.choices) ? result.choices[0] : null).message);
  if (typeof message.content === 'string') result.text = message.content;
  if (Array.isArray(message.tool_calls)) result.tool_calls = message.tool_calls;
  return result;
}
export function normalizeResponse(value: unknown): JsonObject {
  const result: ObjectMap = asObject(value);
  if (result.status === 'failed') throw new OpenAIResponseError({ type: 'response.failed', response: result });
  normalizedUsage(result, openaiFields);
  const output: ObjectMap[] = Array.isArray(result.output) ? result.output.map(object) : [];
  const text = typeof result.output_text === 'string' ? result.output_text : output.filter(item => item.type === 'message').flatMap(item => Array.isArray(item.content) ? item.content : []).map(object).filter(item => item.type === 'output_text' && typeof item.text === 'string').map(item => item.text).join('');
  if (text) result.text = text;
  const calls = output.filter(item => item.type === 'function_call').map(item => Object.fromEntries(['call_id', 'name', 'arguments'].filter(key => key in item).map(key => [key, item[key]])));
  if (calls.length) result.tool_calls = calls;
  return result;
}
async function* responsesStream(stream: AsyncIterable<unknown> | Iterable<unknown>): AsyncIterable<JsonObject> {
  let complete = false, text = '';
  try {
    for await (const event of stream) {
      const raw = asObject(event), chunk: JsonObject = { event: raw };
      if (raw.type === 'response.output_text.delta' && typeof raw.delta === 'string') { text += raw.delta; chunk.delta = { text: raw.delta }; }
      if (raw.type === 'response.failed' || raw.type === 'error') throw new OpenAIResponseError(raw);
      if (raw.type === 'response.completed' || raw.type === 'response.incomplete') { chunk.result = raw.response === undefined || raw.response === null ? { text } : normalizeResponse(raw.response); complete = true; }
      yield chunk;
    }
    if (!complete) throw new OpenAIResponseError({ type: 'response.stream_ended' });
  } catch (error) { throw unknownOutcome(error); }
}
async function* chatStream(stream: AsyncIterable<unknown> | Iterable<unknown>): AsyncIterable<JsonObject> {
  const result: ObjectMap = { text: '' }, calls = new Map<number, ObjectMap>();
  try {
    for await (const event of stream) {
      const raw: ObjectMap = asObject(event), chunk: JsonObject = { event: raw };
      for (const key of ['id', 'model']) if (typeof raw[key] === 'string') result[key] = raw[key];
      if (raw.usage && typeof raw.usage === 'object') { const usage: ObjectMap = { usage: raw.usage }; normalizedUsage(usage, openaiFields); delete result.usage; Object.assign(result, usage); }
      const choice = object(Array.isArray(raw.choices) ? raw.choices[0] : null), delta = object(choice.delta);
      if (typeof choice.finish_reason === 'string') result.finish_reason = choice.finish_reason;
      if (typeof delta.content === 'string') { result.text += delta.content; chunk.delta = { text: delta.content }; }
      for (const fragment of Array.isArray(delta.tool_calls) ? delta.tool_calls.map(object) : []) {
        const index = valid(fragment.index) ? fragment.index : 0;
        const call = calls.get(index) ?? { function: { name: '', arguments: '' } };
        for (const key of ['id', 'type']) if (typeof fragment[key] === 'string') call[key] = fragment[key];
        for (const key of ['name', 'arguments']) if (typeof object(fragment.function)[key] === 'string') call.function[key] += fragment.function[key];
        calls.set(index, call);
      }
      yield chunk;
    }
    if (result.finish_reason === undefined) throw new OpenAIResponseError({ type: 'chat.completion.stream_ended' });
    if (calls.size) result.tool_calls = [...calls].sort(([a], [b]) => a - b).map(([, value]) => value);
    yield { result };
  } catch (error) { throw unknownOutcome(error); }
}
/** OpenAI and Azure OpenAI Responses API. Use with run.modelCallAsync(). */
export function makeResponsesFn(client: { responses: { create: (params: any) => any } }, options: AdapterOptions = {}): ProviderStep {
  const defaults = snapshot(options.defaults ?? {}), stream = options.stream ?? false;
  return async payload => { const params = request(defaults, payload); if (stream) params.stream = true;
    try { const response = await client.responses.create(params); return stream ? responsesStream(response) : normalizeResponse(response); } catch (error) { throw unknownOutcome(error); }
  };
}
/** OpenAI-compatible Chat Completions, including caller-configured routing proxies. */
export function makeChatCompletionsFn(client: { chat: { completions: { create: (params: any) => any } } }, options: AdapterOptions = {}): ProviderStep {
  const defaults = snapshot(options.defaults ?? {}), stream = options.stream ?? false;
  return async payload => { const params = request(defaults, payload); if (stream) { params.stream = true; params.stream_options ??= { include_usage: true }; }
    try { const response = await client.chat.completions.create(params); return stream ? chatStream(response) : normalizeChatCompletion(response); } catch (error) { throw unknownOutcome(error); }
  };
}
/** LiteLLM proxy or any injected completion function; no Python dependency. */
export function makeCompletionFn(completion: (params: JsonObject) => unknown, options: AdapterOptions = {}): ProviderStep {
  return makeChatCompletionsFn({ chat: { completions: { create: completion } } }, options);
}

export function normalizeMessage(value: unknown): JsonObject {
  const result: ObjectMap = asObject(value); normalizedUsage(result, [['input_tokens'], ['output_tokens']], ['cache_creation_input_tokens', 'cache_read_input_tokens']);
  const blocks = Array.isArray(result.content) ? result.content.map(object) : [];
  const text = blocks.filter(block => block.type === 'text' && typeof block.text === 'string').map(block => block.text).join('');
  if (text) result.text = text;
  const calls = blocks.filter(block => block.type === 'tool_use').map(block => Object.fromEntries(['id', 'name', 'input'].filter(key => key in block).map(key => [key, block[key]])));
  if (calls.length) result.tool_calls = calls;
  return result;
}
function parseTool(call: ObjectMap, field: string): ObjectMap { const result = { ...call }; if (typeof result[field] === 'string') { try { result[field] = JSON.parse(result[field] || '{}'); } catch { /* Preserve malformed fragments for audit. */ } } return result; }
async function* messagesStream(stream: AsyncIterable<unknown> | Iterable<unknown>): AsyncIterable<JsonObject> {
  const result: ObjectMap = { text: '' }, tools = new Map<number, ObjectMap>(); let complete = false, inputValid = false, outputValid = false;
  let providerUsage: ObjectMap = {};
  try {
    for await (const event of stream) {
      const raw: ObjectMap = asObject(event), chunk: JsonObject = { event: raw }, delta = object(raw.delta);
      if (raw.type === 'error') throw new AnthropicStreamError(raw);
      if (raw.type === 'message_start') {
        const message = object(raw.message);
        for (const key of ['id', 'model']) if (typeof message[key] === 'string') result[key] = message[key];
        providerUsage = { ...providerUsage, ...object(message.usage) };
        inputValid = valid(providerUsage.input_tokens) && ['cache_creation_input_tokens', 'cache_read_input_tokens'].every(key => !(key in providerUsage) || valid(providerUsage[key]));
      }
      if (raw.type === 'content_block_start') {
        const block = object(raw.content_block);
        if (block.type === 'text' && typeof block.text === 'string') result.text += block.text;
        if (block.type === 'tool_use' && valid(raw.index)) { const tool: ObjectMap = { input: '' }; for (const key of ['id', 'name']) if (typeof block[key] === 'string') tool[key] = block[key]; tools.set(raw.index, tool); }
      }
      if (raw.type === 'content_block_delta') {
        if (delta.type === 'text_delta' && typeof delta.text === 'string') { result.text += delta.text; chunk.delta = { text: delta.text }; }
        if (delta.type === 'input_json_delta' && typeof delta.partial_json === 'string' && tools.has(raw.index)) tools.get(raw.index)!.input += delta.partial_json;
      }
      if (raw.type === 'message_delta') {
        if (typeof delta.stop_reason === 'string') result.stop_reason = delta.stop_reason;
        if (raw.usage && typeof raw.usage === 'object') { providerUsage = { ...providerUsage, ...raw.usage }; outputValid = valid(raw.usage.output_tokens); }
      }
      if (raw.type === 'message_stop') complete = true;
      yield chunk;
    }
    if (!complete) throw new AnthropicStreamError({ type: 'message.stream_ended' });
    if (Object.keys(providerUsage).length) result.provider_usage = providerUsage;
    if (inputValid && outputValid) { const normalized: ObjectMap = { usage: providerUsage }; normalizedUsage(normalized, [['input_tokens'], ['output_tokens']], ['cache_creation_input_tokens', 'cache_read_input_tokens']); if (normalized.usage) result.usage = normalized.usage; }
    if (tools.size) result.tool_calls = [...tools].sort(([a], [b]) => a - b).map(([, tool]) => { const parsed = parseTool(tool, 'input'); if (typeof parsed.input === 'string') { parsed.input_json = parsed.input; delete parsed.input; } return parsed; });
    yield { result };
  } catch (error) { throw unknownOutcome(error); }
}
export function makeMessagesFn(client: { messages: { create: (params: any) => any; countTokens?: (params: any) => any } }, options: AdapterOptions = {}): EstimatingProviderStep {
  const defaults = snapshot(options.defaults ?? {}), stream = options.stream ?? false;
  const call: ProviderStep = async payload => { const params = request(defaults, payload); if (stream) params.stream = true;
    try { const response = await client.messages.create(params); return stream ? messagesStream(response) : normalizeMessage(response); } catch (error) { throw unknownOutcome(error); }
  };
  return Object.assign(call, { async estimateInputTokens(payload: IdentityPayload): Promise<number | null> {
    if (!client.messages.countTokens) throw new TypeError('client.messages.countTokens is required for the explicit network estimator');
    const params = request(defaults, payload), allowed = ['model', 'messages', 'system', 'thinking', 'tool_choice', 'tools', 'output_config', 'cache_control'];
    const counted = await client.messages.countTokens(Object.fromEntries(allowed.filter(key => key in params).map(key => [key, params[key]])));
    return valid(counted) ? counted : valid(object(counted).input_tokens) ? object(counted).input_tokens : null;
  } });
}

function bedrockNormalizeUsage(result: ObjectMap): void {
  const raw = result.usage;
  const names = [['inputTokens', 'input_tokens'], ['outputTokens', 'output_tokens']];
  const extraGroups = [['cacheReadInputTokens', 'cache_read_input_tokens'], ['cacheWriteInputTokens', 'cache_write_input_tokens']];
  normalizedUsage(result, names);
  if (!result.usage) return;
  if (extraGroups.flat().some(key => key in raw && !valid(raw[key]))) { delete result.usage; return; }
  const input = result.usage.input_tokens + extraGroups.reduce((sum, keys) => sum + integer(raw, ...keys), 0);
  if (!Number.isSafeInteger(input)) { delete result.usage; return; }
  result.usage.input_tokens = input;
}
export function normalizeConverse(value: unknown): JsonObject {
  const result: ObjectMap = asObject(value); bedrockNormalizeUsage(result);
  const content = object(object(result.output).message).content;
  const blocks = Array.isArray(content) ? content.map(object) : [];
  const text = blocks.filter(block => typeof block.text === 'string').map(block => block.text).join('');
  if (text) result.text = text;
  const calls = blocks.filter(block => block.toolUse && typeof block.toolUse === 'object').map(block => Object.fromEntries(['toolUseId', 'name', 'input'].filter(key => key in block.toolUse).map(key => [key, block.toolUse[key]])));
  if (calls.length) result.tool_calls = calls;
  return result;
}
async function* converseStream(stream: AsyncIterable<unknown> | Iterable<unknown>): AsyncIterable<JsonObject> {
  const result: ObjectMap = { text: '' }, tools = new Map<number, ObjectMap>(); let complete = false;
  try {
    for await (const event of stream) {
      const raw: ObjectMap = asObject(event), chunk: JsonObject = { event: raw };
      for (const key of Object.keys(raw)) if (key.endsWith('Exception')) throw new BedrockStreamError(key, raw);
      if (typeof object(raw.messageStart).role === 'string') result.role = raw.messageStart.role;
      const start = object(raw.contentBlockStart), tool = object(object(start.start).toolUse);
      if (Object.keys(tool).length && valid(start.contentBlockIndex)) tools.set(start.contentBlockIndex, { toolUseId: tool.toolUseId ?? '', name: tool.name ?? '', input: '' });
      const block = object(raw.contentBlockDelta), delta = object(block.delta);
      if (typeof delta.text === 'string') { result.text += delta.text; chunk.delta = { text: delta.text }; }
      if (typeof object(delta.toolUse).input === 'string' && valid(block.contentBlockIndex)) {
        const call = tools.get(block.contentBlockIndex) ?? { toolUseId: '', name: '', input: '' }; call.input += delta.toolUse.input; tools.set(block.contentBlockIndex, call);
        chunk.delta = { tool_call: { index: block.contentBlockIndex, input: delta.toolUse.input } };
      }
      if (typeof object(raw.messageStop).stopReason === 'string') { result.stopReason = raw.messageStop.stopReason; complete = true; }
      const metadata = object(raw.metadata);
      if (metadata.usage && typeof metadata.usage === 'object') { const usage: ObjectMap = { usage: metadata.usage }; bedrockNormalizeUsage(usage); delete result.usage; Object.assign(result, usage); }
      if (metadata.metrics && typeof metadata.metrics === 'object') result.metrics = metadata.metrics;
      yield chunk;
    }
    if (!complete) throw new BedrockStreamError('streamEnded', { streamEnded: {} });
    if (tools.size) result.tool_calls = [...tools].sort(([a], [b]) => a - b).map(([, tool]) => parseTool(tool, 'input'));
    yield { result };
  } catch (error) { throw unknownOutcome(error); }
}
export interface BedrockClient { converse?: (params: any) => any; converseStream?: (params: any) => any; countTokens?: (params: any) => any; }
/** Accepts the AWS SDK v3 BedrockRuntime aggregate client or caller-owned command wrappers. */
export function makeConverseFn(client: BedrockClient, options: AdapterOptions & { countTokens?: boolean } = {}): EstimatingProviderStep {
  const defaults = snapshot(options.defaults ?? {}), stream = options.stream ?? false, countTokens = options.countTokens ?? false;
  const call: ProviderStep = async payload => {
    const params = request(defaults, payload), invoke = stream ? client.converseStream : client.converse;
    if (!invoke) throw new TypeError(`client.${stream ? 'converseStream' : 'converse'} is required`);
    try { const response = await invoke.call(client, params); if (!stream) return normalizeConverse(response); if (!object(response).stream) throw new TypeError('Bedrock response is missing stream'); return converseStream(response.stream); } catch (error) { throw unknownOutcome(error); }
  };
  return Object.assign(call, { async estimateInputTokens(payload: IdentityPayload): Promise<number | null> {
    if (!countTokens) return null;
    if (!client.countTokens) throw new TypeError('client.countTokens is required');
    const params = request(defaults, payload);
    if (typeof params.modelId !== 'string') throw new TypeError('Bedrock CountTokens requires modelId');
    const keys = ['messages', 'system', 'toolConfig', 'additionalModelRequestFields'];
    const response = await client.countTokens({ modelId: params.modelId, input: { converse: Object.fromEntries(keys.filter(key => key in params).map(key => [key, params[key]])) } });
    if (!valid(object(response).inputTokens)) throw new TypeError('Bedrock CountTokens response is missing valid inputTokens');
    return response.inputTokens;
  } });
}
export const makeAsyncResponsesFn = makeResponsesFn;
export const makeAsyncChatCompletionsFn = makeChatCompletionsFn;
export const makeAsyncMessagesFn = makeMessagesFn;
export const makeAsyncCompletionFn = makeCompletionFn;
