import { loadTelemetryAPI } from './otel-api.cjs';
import { IdentityPayload, JsonObject } from './identity.js';
import { IntegrityError, Node, Store, validateNode } from './tree.js';

export type SpanAttributes = Record<string, string | number | boolean>;
export interface SpanLike { end(): void; }
export interface TracerLike {
  startSpan(name: string, options?: { attributes?: SpanAttributes }, context?: any): SpanLike;
}
export interface TelemetryAPI {
  context: { active(): any };
  trace: { setSpan(context: any, span: any): any };
}

function record(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function provider(payload: IdentityPayload, model: unknown): string | undefined {
  if (record(payload._pollard) && typeof payload._pollard.provider === 'string') return payload._pollard.provider;
  if (typeof model !== 'string') return undefined;
  const prefixes: Record<string, string> = { 'azure/': 'azure.ai.openai', 'bedrock/': 'aws.bedrock', 'vertex_ai/': 'gcp.vertex_ai', 'gemini/': 'gcp.gemini', 'anthropic/': 'anthropic', 'openai/': 'openai' };
  return Object.entries(prefixes).find(([prefix]) => model.startsWith(prefix))?.[1];
}
function spanName(node: Node): string {
  if (node.kind === 'model_call') return `chat ${node.payload.model ?? node.payload.modelId ?? 'model'}`;
  if (node.kind === 'tool_call') return `execute_tool ${node.payload.tool ?? 'tool'}`;
  return `pollard ${node.kind}`;
}

/** Content-free Pollard and GenAI semantic attributes; prompts and results stay private. */
export function spanAttributes(node: Node): SpanAttributes {
  const attributes: SpanAttributes = {
    'pollard.node.id': node.id, 'pollard.node.kind': node.kind,
    'pollard.node.attempt': node.attempt, 'pollard.node.pruned': node.meta.pruned === true,
  };
  if (node.resultDigest !== null) attributes['pollard.result.digest'] = node.resultDigest;
  const registryDigest = node.payload.registry_digest ?? node.meta.registry_digest;
  if (typeof registryDigest === 'string') attributes['pollard.registry.digest'] = registryDigest;
  for (const group of ['charges', 'avoided']) {
    const values = node.meta[group];
    if (record(values)) for (const [name, value] of Object.entries(values)) {
      if (typeof value === 'number' && Number.isFinite(value)) attributes[`pollard.${group === 'charges' ? 'charge' : group}.${name}`] = value;
    }
  }
  if (node.kind === 'refusal' && typeof node.payload.reason === 'string') attributes['pollard.refusal.reason'] = node.payload.reason;
  if (node.kind === 'model_call') {
    attributes['gen_ai.operation.name'] = 'chat';
    const model = node.payload.model ?? node.payload.modelId;
    if (typeof model === 'string') attributes['gen_ai.request.model'] = model;
    const providerName = provider(node.payload, model);
    if (providerName) attributes['gen_ai.provider.name'] = providerName;
    if (record(node.result) && typeof node.result.model === 'string') attributes['gen_ai.response.model'] = node.result.model;
    const usage = record(node.meta.usage) ? node.meta.usage : record(node.result) ? node.result.usage : undefined;
    if (record(usage)) for (const key of ['input_tokens', 'output_tokens']) {
      const value = usage[key];
      if (typeof value === 'number' && Number.isSafeInteger(value)) attributes[`gen_ai.usage.${key}`] = value;
    }
  }
  return attributes;
}

/** Export a correctly parented tree without recursion, using the standard OpenTelemetry API. */
export function exportSpans(store: Store, rootId: string, tracer: TracerLike, api?: TelemetryAPI): number {
  const telemetry = api ?? loadTelemetryAPI() as TelemetryAPI;
  type Frame = { kind: 'enter'; id: string; parentId?: string; context: any } | { kind: 'exit'; span: SpanLike };
  const pending: Frame[] = [{ kind: 'enter', id: rootId, context: telemetry.context.active() }];
  const active: SpanLike[] = [];
  const seen = new Set<string>();
  let count = 0;
  try {
    while (pending.length) {
      const frame = pending.pop()!;
      if (frame.kind === 'exit') { active.pop(); frame.span.end(); continue; }
      if (seen.has(frame.id)) throw new IntegrityError('OpenTelemetry tree contains a cycle or repeated child');
      seen.add(frame.id);
      const node = store.get(frame.id);
      validateNode(node);
      if (node.id !== frame.id || (frame.parentId !== undefined && node.parent !== frame.parentId)) throw new IntegrityError('OpenTelemetry tree lookup or parent mismatch');
      const span = tracer.startSpan(spanName(node), { attributes: spanAttributes(node) }, frame.context);
      active.push(span); count++;
      pending.push({ kind: 'exit', span });
      const context = telemetry.trace.setSpan(frame.context, span);
      for (const child of store.children(node.id).slice().reverse()) pending.push({ kind: 'enter', id: child, parentId: node.id, context });
    }
  } finally {
    let failure: unknown;
    while (active.length) { try { active.pop()!.end(); } catch (error) { failure ??= error; } }
    if (failure) throw failure;
  }
  return count;
}

/** Runtime onNode hook; detached spans carry the Pollard parent id as an attribute. */
export function liveSpanHook(tracer: TracerLike): (node: Node) => void {
  return node => {
    const attributes = spanAttributes(node);
    if (node.parent !== null) attributes['pollard.parent.id'] = node.parent;
    tracer.startSpan(spanName(node), { attributes }).end();
  };
}
