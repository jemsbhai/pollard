import { canonicalText, codePointCompare, deepFreeze, JsonObject, JsonValue, IdentityPayload, nodeId, nonnegativeInteger, objectPayload, resultDigestFromText, resultTextAndDigest, resultToText, snapshot } from './identity.js';

export class IntegrityError extends Error { override name = 'IntegrityError'; }
export class MissingNodeError extends Error { override name = 'MissingNodeError'; }
export class DuplicateRecordingError extends Error { override name = 'DuplicateRecordingError'; }
export class ConcurrentCallError extends Error { override name = 'ConcurrentCallError'; }
export class UsageError extends Error { override name = 'UsageError'; }
export class UnsupportedSchema extends Error { override name = 'UnsupportedSchema'; }
export class PolicyViolation extends Error {
  override name = 'PolicyViolation';
  constructor(message: string, public readonly nodeId: string) { super(message); }
}
export class BudgetExceeded extends Error {
  override name = 'BudgetExceeded';
  constructor(message: string, public readonly nodeId: string) { super(message); }
}
export class ConfirmationRequired extends Error {
  override name = 'ConfirmationRequired';
  constructor(public readonly token: string) { super('confirmation required by policy'); }
}

export type NodeKind = 'root' | 'model_call' | 'tool_call' | 'note' | 'refusal';
const KINDS = new Set<NodeKind>(['root', 'model_call', 'tool_call', 'note', 'refusal']);
const HEX64 = /^[a-f0-9]{64}$/;
export interface NodeInput {
  kind: NodeKind;
  parent: string | null;
  payload: IdentityPayload;
  attempt?: number;
  result?: JsonValue;
  meta?: JsonObject;
}
/** Exact persisted result text is authoritative for its digest. */
export interface StorageRecord {
  id: string;
  kind: NodeKind;
  parent: string | null;
  attempt: number;
  payload: IdentityPayload;
  result_text: string | null;
  result_digest: string | null;
  meta: JsonObject;
}

export class Node {
  readonly id: string;
  readonly kind: NodeKind;
  readonly parent: string | null;
  readonly attempt: number;
  readonly payload: IdentityPayload;
  readonly result: JsonValue;
  readonly resultText: string | null;
  readonly resultDigest: string | null;
  readonly meta: JsonObject;

  private constructor(record: StorageRecord) {
    if (!HEX64.test(record.id)) throw new IntegrityError('node id must be 64 lowercase hex characters');
    if (!KINDS.has(record.kind)) throw new IntegrityError('unsupported node kind');
    if (record.kind === 'root' ? record.parent !== null : typeof record.parent !== 'string' || !HEX64.test(record.parent)) throw new IntegrityError('invalid parent');
    nonnegativeInteger(record.attempt, 'attempt');
    objectPayload(record.payload);
    if (!record.meta || Array.isArray(record.meta) || typeof record.meta !== 'object') throw new TypeError('meta must be a JSON object');
    this.id = record.id;
    this.kind = record.kind;
    this.parent = record.parent;
    this.attempt = record.attempt;
    this.payload = deepFreeze(snapshot(record.payload, true));
    this.meta = deepFreeze(snapshot(record.meta));
    this.resultText = record.result_text;
    this.resultDigest = record.result_digest;
    if (this.resultText === null) {
      if (this.resultDigest !== null) throw new IntegrityError('result digest has no stored result text');
      this.result = null;
    } else {
      if (typeof this.resultText !== 'string' || typeof this.resultDigest !== 'string' || !HEX64.test(this.resultDigest)) throw new IntegrityError('invalid result storage');
      if (resultDigestFromText(this.resultText) !== this.resultDigest) throw new IntegrityError('result digest does not match stored result text');
      this.result = deepFreeze(snapshot(JSON.parse(this.resultText) as JsonValue));
    }
    if (this.id !== this.expectedId) throw new IntegrityError('node id does not match identity fields');
    Object.freeze(this);
  }

  static make(input: NodeInput): Node {
    const attempt = input.attempt ?? 0;
    const [text, digest] = input.result === undefined || input.result === null ? [null, null] : resultTextAndDigest(input.result);
    return new Node({ id: nodeId(input.kind, input.parent, attempt, input.payload), kind: input.kind, parent: input.parent,
      attempt, payload: input.payload, result_text: text, result_digest: digest, meta: input.meta ?? {} });
  }
  static fromStorage(record: StorageRecord): Node { return new Node(record); }
  get expectedId(): string { return nodeId(this.kind, this.parent, this.attempt, this.payload); }
  toStorage(): StorageRecord {
    return { id: this.id, kind: this.kind, parent: this.parent, attempt: this.attempt, payload: snapshot(this.payload, true),
      result_text: this.resultText, result_digest: this.resultDigest, meta: snapshot(this.meta) };
  }
}

/** The frozen seven-method Pollard store contract; implementations return detached snapshots. */
export interface Store {
  put(node: Node): void;
  get(id: string): Node;
  exists(id: string): boolean;
  children(id: string): string[];
  updateMeta(id: string, patch: JsonObject): void;
  walk(rootId: string): Iterable<Node>;
  roots(): string[];
}
/** Optional capability required for live dispatch, separate from the frozen Store contract. */
export interface RecordingStore extends Store { finalize(node: Node): void; }

export function validateNode(node: Node): void {
  const verified = Node.fromStorage(node.toStorage());
  if (resultToText(node.result) !== resultToText(verified.result)) throw new IntegrityError('result does not match stored result text');
}

export class MemoryStore implements RecordingStore {
  #nodes = new Map<string, StorageRecord>();
  #finalizable = new Set<string>();
  put(node: Node): void {
    validateNode(node);
    if (node.parent !== null && !this.#nodes.has(node.parent)) throw new MissingNodeError(node.parent);
    const existing = this.#nodes.get(node.id);
    if (existing) {
      if (canonicalText(existing.payload) !== canonicalText(node.payload) || existing.kind !== node.kind || existing.parent !== node.parent || existing.attempt !== node.attempt) throw new IntegrityError('node id collision');
      if (node.resultText !== null && existing.result_text !== node.resultText) throw new IntegrityError('append-only result conflict');
      return;
    }
    this.#nodes.set(node.id, node.toStorage());
    if (node.resultText === null && node.meta.state === 'pending') this.#finalizable.add(node.id);
  }
  get(id: string): Node {
    const stored = this.#nodes.get(id);
    if (!stored) throw new MissingNodeError(id);
    return Node.fromStorage(stored);
  }
  exists(id: string): boolean { return this.#nodes.has(id); }
  children(id: string): string[] {
    return [...this.#nodes.values()].filter(n => n.parent === id).sort((a, b) => codePointCompare(a.kind, b.kind) || codePointCompare(a.id, b.id)).map(n => n.id);
  }
  updateMeta(id: string, patch: JsonObject): void {
    if (!patch || Array.isArray(patch) || typeof patch !== 'object') throw new TypeError('meta patch must be an object');
    const node = this.get(id).toStorage();
    node.meta = snapshot({ ...node.meta, ...patch });
    this.#nodes.set(id, node);
    if (Object.hasOwn(patch, 'state') && patch.state !== 'pending') this.#finalizable.delete(id);
  }
  *walk(rootId: string): Iterable<Node> {
    const pending = [rootId];
    const seen = new Set<string>();
    while (pending.length) {
      const id = pending.pop()!;
      if (seen.has(id)) throw new IntegrityError('cycle in store traversal');
      seen.add(id);
      yield this.get(id);
      pending.push(...this.children(id).reverse());
    }
  }
  roots(): string[] {
    return [...this.#nodes.values()].filter(n => n.parent === null).sort((a, b) => codePointCompare(String(a.payload.run ?? ''), String(b.payload.run ?? '')) || codePointCompare(a.id, b.id)).map(n => n.id);
  }
  finalize(node: Node): void {
    validateNode(node);
    const existing = this.get(node.id);
    if (!this.#finalizable.has(node.id) || existing.resultText !== null || existing.meta.state !== 'pending' || node.meta.state !== 'completed') throw new IntegrityError('only a pending dispatch can be finalized once');
    if (existing.expectedId !== node.expectedId) throw new IntegrityError('finalization changes identity');
    this.#finalizable.delete(node.id);
    this.#nodes.set(node.id, node.toStorage());
  }
}

export interface VerifyFinding { nodeId: string; message: string; }
export interface VerifyReport { ok: boolean; findings: VerifyFinding[]; }
export function verify(store: Store, nodeId: string): VerifyReport {
  const findings: VerifyFinding[] = [];
  const seen = new Set<string>();
  let id: string | null = nodeId;
  while (id !== null) {
    if (seen.has(id)) { findings.push({ nodeId: id, message: 'cycle detected in ancestry' }); break; }
    seen.add(id);
    let node: Node;
    try { node = store.get(id); } catch (error) { findings.push({ nodeId: id, message: error instanceof Error ? error.message : 'node is missing' }); break; }
    if (node.id !== id) findings.push({ nodeId: id, message: 'store returned a node with a different id' });
    try { validateNode(node); } catch (error) { findings.push({ nodeId: id, message: error instanceof Error ? error.message : 'invalid node' }); }
    id = node.parent;
  }
  return { ok: findings.length === 0, findings };
}
