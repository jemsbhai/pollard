import { IdentityPayload, JsonObject, JsonValue, objectPayload, snapshot } from './identity.js';
import { ActionSpec, Registry } from './registry.js';
import { UnsupportedSchema } from './tree.js';
import { markPostDispatchOutcomeUnknown } from './streaming.js';

/** Structural interface accepted by the JavaScript MCP SDK and lightweight clients. */
export interface MCPSession {
  listTools?(params?: { cursor?: string }): unknown | Promise<unknown>;
  callTool?(params: { name: string; arguments: IdentityPayload }): unknown | Promise<unknown>;
  list_tools?(): unknown | Promise<unknown>;
  call_tool?(name: string, args: IdentityPayload): unknown | Promise<unknown>;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError(`${label} must be an object`);
  return value as Record<string, unknown>;
}

function jsonable(value: unknown, active = new Set<object>()): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number') return value;
  if (!value || typeof value !== 'object') throw new TypeError('MCP result contains a non-JSON value');
  if (active.has(value)) throw new TypeError('MCP result contains a cycle');
  active.add(value);
  try {
    if (Array.isArray(value)) return value.map(child => jsonable(child, active));
    const candidate = value as Record<string, unknown>;
    if (typeof candidate.model_dump === 'function') return jsonable(candidate.model_dump(), active);
    if (typeof candidate.toJSON === 'function') return jsonable(candidate.toJSON(), active);
    return Object.fromEntries(Object.entries(candidate).map(([key, child]) => [key, jsonable(child, active)]));
  } finally { active.delete(value); }
}

/** Build governed, side-effecting actions from MCP tools/list; every call stays behind Runtime. */
export async function registryFromMCP(session: MCPSession, options: { exclude?: Iterable<string> } = {}): Promise<Registry> {
  if (typeof session.listTools !== 'function' && typeof session.list_tools !== 'function') throw new TypeError('MCP session lacks listTools');
  if (typeof session.callTool !== 'function' && typeof session.call_tool !== 'function') throw new TypeError('MCP session lacks callTool');
  const excluded = new Set(options.exclude ?? []);
  const specs: ActionSpec[] = [];
  const cursors = new Set<string>();
  let cursor: string | undefined;
  do {
    const listing = record(await (session.listTools ? session.listTools(cursor === undefined ? undefined : { cursor }) : session.list_tools!()), 'MCP tools/list response');
    if (!Array.isArray(listing.tools)) throw new TypeError('MCP tools/list response must contain a tools list');
    for (const raw of listing.tools) {
      const tool = record(raw, 'MCP tool');
      if (typeof tool.name !== 'string' || !tool.name) throw new TypeError('MCP tool missing string field name');
      const name = tool.name;
      if (excluded.has(name)) continue;
      const schema = tool.inputSchema ?? tool.input_schema ?? {};
      objectPayload(schema, `MCP tool ${name} schema`);
      try {
        specs.push(new ActionSpec({
          name, version: 'mcp', description: typeof tool.description === 'string' ? tool.description : '',
          schema, sideEffects: true,
          handler: async args => {
            try {
              const result = await (session.callTool ? session.callTool({ name, arguments: args }) : session.call_tool!(name, args));
              const normalized = jsonable(result);
              return snapshot(normalized !== null && typeof normalized === 'object' && !Array.isArray(normalized) ? normalized : { result: normalized }) as JsonObject;
            } catch (error) { throw markPostDispatchOutcomeUnknown(error); }
          },
        }));
      } catch (error) {
        if (error instanceof UnsupportedSchema) throw new UnsupportedSchema(`MCP tool ${name}: ${error.message}`);
        throw error;
      }
    }
    if (listing.nextCursor !== undefined && (typeof listing.nextCursor !== 'string' || !listing.nextCursor)) throw new TypeError('MCP tools/list nextCursor must be a nonempty string');
    cursor = listing.nextCursor as string | undefined;
    if (cursor !== undefined) {
      if (!session.listTools) throw new TypeError('MCP pagination requires listTools');
      if (cursors.has(cursor)) throw new TypeError('MCP tools/list repeated a pagination cursor');
      cursors.add(cursor);
    }
  } while (cursor !== undefined);
  return new Registry(specs);
}
