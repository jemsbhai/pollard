import { canonicalText, codePointCompare, deepFreeze, IdentityPayload, JsonObject, JsonValue, objectPayload, snapshot } from './identity.js';
import type { Charges } from './meters.js';

export const REPLAY_CONTRACT_FORMAT = 'pollard/replay-contract/v1';
export const REVALIDATION_FORMAT = 'pollard/revalidation/v1';
export const MAX_DIFFERENCE_PATHS = 100;
export interface ReplayContractOptions {
  provider: string; modelRevision?: string; apiVersion?: string; adapter?: string; adapterVersion?: string;
  sdk?: string; sdkVersion?: string; applicationRevision?: string; environment?: IdentityPayload;
}
function nonempty(value: unknown, name: string): asserts value is string { if (typeof value !== 'string' || !value.trim()) throw new TypeError(`${name} must be a nonempty string`); }
export class ReplayContract {
  readonly #document: IdentityPayload;
  constructor(options: ReplayContractOptions) {
    nonempty(options.provider, 'provider');
    const doc: IdentityPayload = { format: REPLAY_CONTRACT_FORMAT, provider: options.provider };
    for (const [key, field] of Object.entries({ modelRevision: 'model_revision', apiVersion: 'api_version', adapter: 'adapter', adapterVersion: 'adapter_version', sdk: 'sdk', sdkVersion: 'sdk_version', applicationRevision: 'application_revision' })) {
      const value = options[key as keyof ReplayContractOptions];
      if (value !== undefined) { nonempty(value, key); doc[field] = value; }
    }
    if (options.environment !== undefined) { objectPayload(options.environment, 'environment'); if (Object.keys(options.environment).length) doc.environment = snapshot(options.environment, true); }
    this.#document = deepFreeze(doc); Object.freeze(this);
  }
  get provider(): string { return this.#document.provider as string; }
  toDict(): IdentityPayload { return snapshot(this.#document, true); }
  bind(payload: IdentityPayload): IdentityPayload {
    objectPayload(payload); const bound = snapshot(payload, true), metadata = reserved(bound), contract = this.toDict();
    if (metadata.replay_contract !== undefined && canonicalText(metadata.replay_contract) !== canonicalText(contract)) throw new TypeError('payload is already bound to a different replay contract');
    metadata.replay_contract = contract; bound._pollard = metadata; return bound;
  }
}
export class RevalidationComparison {
  readonly matched: boolean;
  readonly differencePaths: readonly string[];
  readonly truncated: boolean;
  constructor(options: { matched: boolean; differencePaths?: readonly string[]; truncated?: boolean }) {
    if (typeof options.matched !== 'boolean' || (options.truncated !== undefined && typeof options.truncated !== 'boolean')) throw new TypeError('matched and truncated must be boolean');
    const paths = [...(options.differencePaths ?? [])];
    if (paths.length > MAX_DIFFERENCE_PATHS || paths.some(path => typeof path !== 'string' || !path.startsWith('/'))) throw new TypeError('difference paths must contain at most 100 JSON pointers');
    if (options.matched && (paths.length || options.truncated)) throw new TypeError('matched comparisons cannot contain differences');
    this.matched = options.matched; this.differencePaths = Object.freeze(paths); this.truncated = options.truncated ?? false; Object.freeze(this);
  }
  toDict(): IdentityPayload { return { matched: this.matched, difference_paths: [...this.differencePaths], truncated: this.truncated }; }
}
export interface RevalidationComparator { readonly name: string; compare(recorded: JsonObject, live: JsonObject): RevalidationComparison; }
export class ExactResultComparator implements RevalidationComparator {
  readonly name = 'exact-result/v1';
  compare(recorded: JsonObject, live: JsonObject): RevalidationComparison { return compareValues(recorded, live); }
}
export class NormalizedModelComparator implements RevalidationComparator {
  readonly name = 'normalized-model/v1';
  compare(recorded: JsonObject, live: JsonObject): RevalidationComparison { return compareValues(semantics(recorded), semantics(live)); }
}
export interface RevalidationReport {
  observationId: string; recordedNodeId: string; liveNodeId: string; evidenceNodeId: string; comparator: string;
  matched: boolean; exactMatch: boolean; recordedResultDigest: string; liveResultDigest: string;
  differencePaths: readonly string[]; differencesTruncated: boolean; recordedContract: IdentityPayload | null;
  liveContract: IdentityPayload; charges: Charges;
}
function reserved(payload: IdentityPayload): IdentityPayload {
  if (payload._pollard === undefined || payload._pollard === null) return {};
  objectPayload(payload._pollard, 'payload _pollard'); return snapshot(payload._pollard as IdentityPayload, true);
}
export function extractReplayContract(payload: IdentityPayload): IdentityPayload | null {
  const meta = reserved(payload), contract = meta.replay_contract;
  if (contract === undefined) return null;
  objectPayload(contract, 'recorded replay contract'); return snapshot(contract as IdentityPayload, true);
}
export function makeRevalidationPayload(payload: IdentityPayload, options: { observationId: string; recordedNodeId: string; recordedResultDigest: string; contract: ReplayContract; comparatorName: string }): IdentityPayload {
  nonempty(options.observationId, 'observationId'); nonempty(options.comparatorName, 'comparator name');
  const marked = snapshot(payload, true), metadata = reserved(marked);
  if (metadata.revalidation !== undefined) throw new TypeError('payload already contains reserved revalidation metadata');
  metadata.revalidation = { format: REVALIDATION_FORMAT, observation_id: options.observationId, recorded_node_id: options.recordedNodeId, recorded_result_digest: options.recordedResultDigest, live_contract: options.contract.toDict(), comparator: options.comparatorName };
  marked._pollard = metadata; return marked;
}
function semantics(result: JsonObject): JsonObject {
  const names = ['text', 'tool_calls', 'refusal', 'structured_output'];
  if (!names.some(name => Object.hasOwn(result, name))) return Object.fromEntries(Object.entries(result).filter(([key]) => !['usage','provider_usage','chunks'].includes(key)));
  return Object.fromEntries(names.filter(name => Object.hasOwn(result, name)).map(name => [name, name === 'tool_calls' && Array.isArray(result[name]) ? (result[name] as JsonValue[]).map(normalizeCall) : result[name]]));
}
function normalizeCall(value: JsonValue): JsonValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const parse = (text: JsonValue): JsonValue => { if (typeof text !== 'string') return text; try { return snapshot(JSON.parse(text)); } catch { return text; } };
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    if (['id','call_id','toolUseId','index'].includes(key)) continue;
    let entry = item;
    if (key === 'function' && item && typeof item === 'object' && !Array.isArray(item)) { entry = snapshot(item); if (entry.arguments !== undefined) entry.arguments = parse(entry.arguments); }
    else if (key === 'arguments' || key === 'input_json') entry = parse(item);
    Object.defineProperty(result, key, { value: entry, enumerable: true });
  }
  return result;
}
function compareValues(left: JsonValue, right: JsonValue): RevalidationComparison {
  const paths: string[] = []; let truncated = false;
  const add = (path: string) => { if (paths.length >= MAX_DIFFERENCE_PATHS) truncated = true; else paths.push(path || '/'); };
  const pointer = (key: string) => key.replaceAll('~', '~0').replaceAll('/', '~1');
  const visit = (a: JsonValue, b: JsonValue, path: string): void => {
    if (truncated) return;
    if (typeof a !== typeof b || (a === null) !== (b === null) || Array.isArray(a) !== Array.isArray(b)) { add(path); return; }
    if (Array.isArray(a) && Array.isArray(b)) {
      for (let i = 0; i < Math.max(a.length, b.length); i++) { if (i >= a.length || i >= b.length) add(`${path}/${i}`); else visit(a[i], b[i], `${path}/${i}`); if (truncated) break; }
    } else if (a && b && typeof a === 'object' && typeof b === 'object') {
      const aa = a as JsonObject, bb = b as JsonObject;
      for (const key of [...new Set([...Object.keys(aa), ...Object.keys(bb)])].sort(codePointCompare)) { const child = `${path}/${pointer(key)}`; if (!Object.hasOwn(aa, key) || !Object.hasOwn(bb, key)) add(child); else visit(aa[key], bb[key], child); if (truncated) break; }
    } else if (a !== b) add(path);
  };
  visit(left, right, ''); return new RevalidationComparison({ matched: paths.length === 0 && !truncated, differencePaths: paths, truncated });
}
