import { deepFreeze, JsonObject, JsonValue, snapshot } from './identity.js';
function objectPayload(value: unknown, label: string): asserts value is JsonObject { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError(`${label} must be an object`); snapshot(value as JsonObject); }

export type DeltaCallback = (chunk: JsonObject) => void;
export type AsyncDeltaCallback = (chunk: JsonObject) => void | Promise<void>;
export type StepResult = JsonObject | Iterable<JsonObject>;
export type AsyncStepResult = StepResult | AsyncIterable<JsonObject>;
const registryKey = Symbol.for('pollardai/post-dispatch-errors');
const registryRealm = globalThis as unknown as Record<symbol, unknown>;
const existingMarks = registryRealm[registryKey];
if (existingMarks !== undefined && !(existingMarks instanceof WeakSet)) throw new TypeError('invalid Pollard post-dispatch marker registry');
const marked = (existingMarks as WeakSet<object> | undefined) ?? new WeakSet<object>();
if (!existingMarks) Object.defineProperty(globalThis, registryKey, { value: marked });
const marker = Symbol.for('pollardai/post-dispatch-outcome-unknown');
export class PostDispatchOutcomeUnknown extends Error {
  override name = 'PostDispatchOutcomeUnknown';
  constructor(readonly error: unknown) { super('external call outcome is unknown after dispatch'); }
}
export function markPostDispatchOutcomeUnknown(error: unknown): unknown {
  if (error && (typeof error === 'object' || typeof error === 'function')) { marked.add(error); try { Object.defineProperty(error, marker, { value: true }); } catch {} return error; }
  return new PostDispatchOutcomeUnknown(error);
}
export function isPostDispatchOutcomeUnknown(error: unknown): boolean { return error instanceof PostDispatchOutcomeUnknown || (!!error && typeof error === 'object' && (marked.has(error) || (error as Record<symbol, unknown>)[marker] === true || (error as {postDispatchOutcomeUnknown?: boolean}).postDispatchOutcomeUnknown === true)); }
export function mergeStreamValue(target: JsonObject, delta: JsonObject): void {
  for (const [key, value] of Object.entries(delta)) {
    const current = Object.hasOwn(target, key) ? target[key] : undefined;
    if (isObject(current) && isObject(value)) mergeStreamValue(current, value);
    else if (typeof current === 'string' && typeof value === 'string') target[key] = current + value;
    else if (Array.isArray(current) && Array.isArray(value)) target[key] = [...current, ...value];
    else Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
  }
}
function isObject(value: unknown): value is JsonObject { return !!value && typeof value === 'object' && !Array.isArray(value); }
function chunkResult(result: JsonObject, chunk: JsonObject): JsonObject {
  const complete = chunk.result, delta = chunk.delta;
  if (complete !== undefined && complete !== null) { if (!isObject(complete)) throw new TypeError('stream chunk result must be an object'); return snapshot(complete); }
  if (delta !== undefined && delta !== null) { if (!isObject(delta)) throw new TypeError('stream chunk delta must be an object'); mergeStreamValue(result, delta); }
  else mergeStreamValue(result, chunk);
  return result;
}
export function consumeStepResult(value: StepResult, options: { onDelta?: DeltaCallback; keepChunks?: boolean; signal?: AbortSignal } = {}): JsonObject {
  options.signal?.throwIfAborted();
  if (!value || typeof value !== 'object') throw new TypeError('handler must return an object or an iterable of chunk objects');
  if (!(Symbol.iterator in value)) { objectPayload(value, 'handler result'); return snapshot(value as JsonObject); }
  let result: JsonObject = {}, received = false; const chunks: JsonObject[] = [];
  try {
    for (const item of value as Iterable<JsonObject>) {
      received = true; options.signal?.throwIfAborted(); objectPayload(item, 'stream chunk');
      const chunk = snapshot(item); if (options.keepChunks) chunks.push(chunk);
      const callbackResult: unknown = options.onDelta?.(deepFreeze(snapshot(chunk)));
      if (callbackResult && typeof (callbackResult as PromiseLike<unknown>).then === 'function') { Promise.resolve(callbackResult).catch(() => undefined); throw new TypeError('async onDelta requires an async call method'); }
      result = chunkResult(result, snapshot(chunk));
    }
  } catch (error) { throw received ? markPostDispatchOutcomeUnknown(error) : error; }
  if (options.keepChunks) result.chunks = chunks;
  return result;
}
export async function consumeStepResultAsync(value: AsyncStepResult, options: { onDelta?: AsyncDeltaCallback; keepChunks?: boolean; signal?: AbortSignal } = {}): Promise<JsonObject> {
  options.signal?.throwIfAborted();
  if (!value || typeof value !== 'object') throw new TypeError('handler must return an object or an iterable of chunk objects');
  if (!(Symbol.iterator in value) && !(Symbol.asyncIterator in value)) { objectPayload(value, 'handler result'); return snapshot(value as JsonObject); }
  let result: JsonObject = {}, received = false; const chunks: JsonObject[] = [];
  try {
    for await (const item of value as AsyncIterable<JsonObject>) {
      received = true; options.signal?.throwIfAborted(); objectPayload(item, 'stream chunk');
      const chunk = snapshot(item); if (options.keepChunks) chunks.push(chunk);
      await options.onDelta?.(deepFreeze(snapshot(chunk))); options.signal?.throwIfAborted();
      result = chunkResult(result, snapshot(chunk));
    }
  } catch (error) { throw received ? markPostDispatchOutcomeUnknown(error) : error; }
  if (options.keepChunks) result.chunks = chunks;
  return result;
}
export function recordedChunks(result: JsonValue): JsonObject[] { return isObject(result) && Array.isArray(result.chunks) ? result.chunks.filter(isObject).map(chunk => deepFreeze(snapshot(chunk))) : []; }
