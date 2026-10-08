import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionSpec, Registry } from '../dist/esm/registry.js';
import { Runtime } from '../dist/esm/runtime.js';
import { MemoryStore, Node, UnsupportedSchema, PolicyViolation, IntegrityError } from '../dist/esm/tree.js';
import { resolveLocalRefs, schemaHasLocalRefs } from '../dist/esm/schema.js';
import { registryFromMCP } from '../dist/esm/mcp.js';
import { exportSpans, liveSpanHook, spanAttributes } from '../dist/esm/observability.js';
import { isPostDispatchOutcomeUnknown } from '../dist/esm/streaming.js';

const spec = schema => new ActionSpec({ name: 'referenced', version: '1', description: 'Schema', schema, sideEffects: false });

test('local refs expand before digesting, preserve sensitive fields, and handle escaped pointers', () => {
  const schema = {
    $defs: { 'path/name': { type: 'object', properties: { value: { $ref: '#/$defs/til~0de', sensitive: true } } }, 'til~de': { type: 'string' }, 'space key': { type: 'boolean' } },
    type: 'object', properties: { nested: { $ref: '#/$defs/path~1name' }, enabled: { $ref: '#/%24defs/space%20key' } }, required: ['nested', 'enabled'], additionalProperties: false,
  };
  const action = spec(schema);
  assert.equal('$defs' in action.schema, false);
  assert.equal(action.validateArgs({ nested: { value: 'secret' }, enabled: true }), null);
  assert.match(action.validateArgs({ nested: { value: 1 }, enabled: true }), /expected string/);
  assert.equal(typeof action.redactArgs({ nested: { value: 'secret' }, enabled: true }).nested.value.__pollard_redacted, 'string');
  assert.equal(action.specDigest, spec(resolveLocalRefs(schema)).specDigest);
  assert.equal(schema.$defs['til~de'].type, 'string');
  assert.equal(new Registry([action]).has('referenced'), true);
});

test('references inside unions and arrays resolve while literal defaults and enum values remain data', () => {
  const literal = { $ref: 'not-a-schema-reference' };
  const schema = { definitions: { text: { type: 'string' } }, type: 'object', properties: {
    labels: { type: 'array', items: { anyOf: [{ $ref: '#/definitions/text' }, { type: 'null' }] } },
    mode: { default: literal, enum: [literal] },
  } };
  const action = spec(schema);
  assert.equal(action.validateArgs({ labels: ['a', null], mode: literal }), null);
  assert.deepEqual(action.schema.properties.mode.default, literal);
  assert.equal(schemaHasLocalRefs({ type: 'object', default: literal }), false);
});

test('local refs fail closed for missing, cyclic, remote, malformed, nonobject and constrained siblings', () => {
  const bad = [
    [{ $ref: '#/missing' }, /missing local reference/],
    [{ $ref: '#' }, /cyclic local reference/],
    [{ $defs: { a: { $ref: '#/$defs/b' }, b: { $ref: '#/$defs/a' } }, $ref: '#/$defs/a' }, /cyclic local reference/],
    [{ $ref: 'https://example.test/schema' }, /only local JSON Pointer/],
    [{ $ref: '#/foo~2bar' }, /invalid JSON Pointer escape/],
    [{ $ref: '#/%ZZ' }, /invalid percent escape/],
    [{ $ref: '#/%FF' }, /invalid percent escape/],
    [{ $defs: { x: false }, $ref: '#/$defs/x' }, /target must be a schema object/],
    [{ $defs: { x: { type: 'string' } }, $ref: '#/$defs/x', maxLength: 2 }, /unsupported sibling/],
  ];
  for (const [schema, message] of bad) assert.throws(() => spec(schema), error => error instanceof UnsupportedSchema && message.test(error.message));
});

test('reference lookup handles __proto__ as data and cannot read inherited targets', () => {
  const schema = JSON.parse('{"$defs":{"__proto__":{"type":"string"}},"type":"object","properties":{"__proto__":{"$ref":"#/$defs/__proto__"}}}');
  const action = spec(schema);
  assert.equal(Object.hasOwn(action.schema.properties, '__proto__'), true);
  assert.equal(action.validateArgs(JSON.parse('{"__proto__":"ok"}')), null);
  assert.throws(() => spec({ $ref: '#/constructor' }), /missing local reference/);
});

test('MCP JS SDK handlers use governed calls, exclusion, pagination, and local generated schemas', async () => {
  const calls = [], requests = [];
  const session = {
    async listTools(params) {
      requests.push(params);
      return params?.cursor ? { tools: [{ name: 'excluded', inputSchema: { type: 'object', patternProperties: {} } }] } : {
        tools: [{ name: 'search', description: 'Search', inputSchema: { type: 'object', $defs: { query: { type: 'string' } }, properties: { query: { $ref: '#/$defs/query' } }, required: ['query'] } }], nextCursor: 'page2',
      };
    },
    async callTool(params) { calls.push(params); return { content: [{ type: 'text', text: params.arguments.query }], usage: { input_tokens: 0, output_tokens: 0 } }; },
  };
  const registry = await registryFromMCP(session, { exclude: ['excluded'] });
  assert.equal(registry.get('search', 'mcp').sideEffects, true);
  const run = new Runtime({ registry }).run('mcp');
  const node = await run.toolCallAsync('search', { query: 'pollard' });
  assert.equal(node.result.content[0].text, 'pollard');
  assert.deepEqual(calls, [{ name: 'search', arguments: { query: 'pollard' } }]);
  assert.deepEqual(requests, [undefined, { cursor: 'page2' }]);
  assert.throws(() => new Runtime({ registry, policies: [{ decide: () => 'deny' }] }).run('denied').toolCall('search', { query: 'x' }), PolicyViolation);
  assert.equal(calls.length, 1);
});

test('MCP legacy clients and nested SDK result serializers are normalized', async () => {
  class Nested { model_dump() { return { value: 'nested' }; } }
  class Result { toJSON() { return { content: [new Nested()] }; } }
  const registry = await registryFromMCP({ list_tools: () => ({ tools: [{ name: 'inspect' }] }), call_tool: (name, args) => { assert.equal(name, 'inspect'); assert.deepEqual(args, {}); return new Result(); } });
  assert.deepEqual(await registry.get('inspect').handler({}), { content: [{ value: 'nested' }] });
});

test('MCP validates listings before creating a registry and marks uncertain dispatch errors', async () => {
  const callTool = () => ({});
  await assert.rejects(registryFromMCP({ listTools: () => ({ tools: 'bad' }), callTool }), /tools list/);
  await assert.rejects(registryFromMCP({ listTools: () => ({ tools: [{ name: 'bad', inputSchema: { patternProperties: {} } }] }), callTool }), /MCP tool bad/);
  await assert.rejects(registryFromMCP({ listTools: () => ({ tools: [], nextCursor: 'repeat' }), callTool }), /repeated.*cursor/);
  const failure = new Error('connection lost');
  const registry = await registryFromMCP({ listTools: () => ({ tools: [{ name: 'call' }] }), callTool: () => { throw failure; } });
  await assert.rejects(registry.get('call').handler({}), error => error === failure && isPostDispatchOutcomeUnknown(error));
});

function tracing() {
  const spans = [];
  const api = { context: { active: () => ({ ambient: true }) }, trace: { setSpan: (ctx, span) => ({ ...ctx, parent: span }) } };
  const tracer = { startSpan(name, options, context) { const span = { name, attributes: options.attributes, parent: context?.parent, ended: false, end() { this.ended = true; } }; spans.push(span); return span; } };
  return { spans, tracer, api };
}

test('OTel export parents every span, exports usage and charges, and omits prompt/result contents', () => {
  const store = new MemoryStore();
  const root = Node.make({ kind: 'root', parent: null, payload: { run: 'otel' } }); store.put(root);
  const model = Node.make({ kind: 'model_call', parent: root.id, payload: { model: 'openai/demo', prompt: 'private input' }, result: { model: 'demo-2', text: 'private output', usage: { input_tokens: 2, output_tokens: 1 } }, meta: { charges: { steps: 1, usd: 0.02 }, avoided: { tokens: 3 } } }); store.put(model);
  const refusal = Node.make({ kind: 'refusal', parent: root.id, payload: { reason: 'budget' } }); store.put(refusal);
  const { tracer, api, spans } = tracing();
  assert.equal(exportSpans(store, root.id, tracer, api), 3);
  const byId = new Map(spans.map(span => [span.attributes['pollard.node.id'], span]));
  assert.equal(byId.get(model.id).parent, byId.get(root.id));
  assert.equal(byId.get(refusal.id).parent, byId.get(root.id));
  const attributes = byId.get(model.id).attributes;
  assert.equal(attributes['gen_ai.provider.name'], 'openai');
  assert.equal(attributes['gen_ai.usage.input_tokens'], 2);
  assert.equal(attributes['pollard.charge.usd'], 0.02);
  assert.equal(attributes['pollard.avoided.tokens'], 3);
  assert.equal(JSON.stringify(spans, (key, value) => key === 'parent' ? undefined : value).includes('private'), false);
  assert.equal(spans.every(span => span.ended), true);
});

test('OTel export is iterative for deep trees and closes open spans on storage failures', () => {
  const store = new MemoryStore();
  let node = Node.make({ kind: 'root', parent: null, payload: { run: 'deep' } }); store.put(node);
  const root = node;
  for (let index = 0; index < 1200; index++) { node = Node.make({ kind: 'note', parent: node.id, payload: { index } }); store.put(node); }
  const { tracer, api, spans } = tracing();
  assert.equal(exportSpans(store, root.id, tracer, api), 1201);
  assert.equal(spans.every(span => span.ended), true);
  const broken = tracing();
  const source = { get: id => store.get(id), children: () => { throw new Error('storage unavailable'); } };
  assert.throws(() => exportSpans(source, root.id, broken.tracer, broken.api), /storage unavailable/);
  assert.equal(broken.spans[0].ended, true);
  const cyclic = { get: id => store.get(id), children: () => [root.id] };
  assert.throws(() => exportSpans(cyclic, root.id, broken.tracer, broken.api), IntegrityError);
});

test('live OTel hook is detached and includes Pollard parent identity', () => {
  const node = Node.make({ kind: 'model_call', parent: 'a'.repeat(64), payload: { modelId: 'aws-model', _pollard: { provider: 'aws.bedrock' } }, meta: { usage: { input_tokens: 4, output_tokens: 2 } } });
  const { tracer, spans } = tracing();
  liveSpanHook(tracer)(node);
  assert.equal(spans[0].attributes['pollard.parent.id'], node.parent);
  assert.equal(spans[0].attributes['gen_ai.provider.name'], 'aws.bedrock');
  assert.equal(spanAttributes(node)['gen_ai.usage.input_tokens'], 4);
  assert.equal(spans[0].ended, true);
});
