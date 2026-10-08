import { canonicalText, JsonObject, resultToText } from './identity.js';
import { IntegrityError, MemoryStore, Node, RecordingStore, validateNode } from './tree.js';

const PRIME = (1n << 61n) - 1n;
function polynomialHash(data: Uint8Array): bigint {
  let hash = 0n;
  for (const byte of data) hash = (hash * 131n + BigInt(byte + 1)) % PRIME;
  return hash;
}
function nodeRecord(node: Node): JsonObject {
  return { op: 'put', id: node.id, parent: node.parent, kind: node.kind, attempt: node.attempt,
    payload: canonicalText(node.payload), result: node.resultText, result_digest: node.resultDigest, meta: resultToText(node.meta) };
}
function recordNode(record: JsonObject): Node {
  for (const field of ['id', 'kind', 'payload', 'meta']) if (typeof record[field] !== 'string') throw new IntegrityError(`hashrope put record requires string ${field}`);
  if (record.parent !== null && typeof record.parent !== 'string') throw new IntegrityError('hashrope put record has invalid parent');
  if (typeof record.attempt !== 'number') throw new IntegrityError('hashrope put record has invalid attempt');
  if (record.result !== null && typeof record.result !== 'string') throw new IntegrityError('hashrope put record has invalid result text');
  if (record.result_digest !== null && typeof record.result_digest !== 'string') throw new IntegrityError('hashrope put record has invalid result digest');
  try {
    return Node.fromStorage({ id: record.id as string, kind: record.kind as Node['kind'], parent: record.parent as string | null,
      attempt: record.attempt, payload: JSON.parse(record.payload as string), result_text: record.result as string | null,
      result_digest: record.result_digest as string | null, meta: JSON.parse(record.meta as string) });
  } catch (error) { throw new IntegrityError(`invalid hashrope node: ${error instanceof Error ? error.message : 'invalid JSON'}`); }
}

/** Native operation log compatible with Python HashRopeStore and its default polynomial hash. */
export class HashRopeStore implements RecordingStore {
  #store = new MemoryStore();
  #chunks: Buffer[] = [];
  #persisted = new Set<string>();
  constructor(data: Uint8Array = new Uint8Array()) {
    if (!(data instanceof Uint8Array)) throw new TypeError('hashrope data must be bytes');
    let text: string;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(data); }
    catch { throw new IntegrityError('hashrope log is not valid UTF-8'); }
    for (const [index, line] of text.split(/\r?\n/).entries()) {
      if (!line) continue;
      let record: JsonObject;
      try { record = JSON.parse(line); }
      catch { throw new IntegrityError(`hashrope log line ${index + 1} is not JSON`); }
      if (!record || typeof record !== 'object' || Array.isArray(record)) throw new IntegrityError(`hashrope log line ${index + 1} is not an object`);
      if (record.op === 'put') {
        const node = recordNode(record);
        this.#applyPut(node); this.#persisted.add(node.id);
      } else if (record.op === 'meta') {
        if (typeof record.id !== 'string' || !record.patch || typeof record.patch !== 'object' || Array.isArray(record.patch)) throw new IntegrityError(`hashrope log line ${index + 1} has invalid meta patch`);
        this.#store.updateMeta(record.id, record.patch);
      } else throw new IntegrityError(`hashrope log line ${index + 1} has unknown operation`);
    }
    if (data.length) this.#chunks.push(Buffer.from(data));
  }
  #append(record: JsonObject): void {
    if (this.#chunks.length && this.#chunks[this.#chunks.length - 1].at(-1) !== 10) this.#chunks.push(Buffer.from('\n'));
    this.#chunks.push(Buffer.from(resultToText(record) + '\n', 'utf8'));
  }
  #applyPut(node: Node): boolean {
    validateNode(node);
    if (!this.#store.exists(node.id)) { this.#store.put(node); return true; }
    const existing = this.#store.get(node.id);
    if (canonicalText(existing.payload) !== canonicalText(node.payload) || existing.kind !== node.kind || existing.parent !== node.parent || existing.attempt !== node.attempt) throw new IntegrityError('node id collision');
    if (node.resultText === null || existing.resultText === node.resultText) return false;
    const conflicts = existing.meta.result_conflicts ?? [];
    if (!Array.isArray(conflicts)) throw new IntegrityError('stored result conflicts must be an array');
    this.#store.updateMeta(node.id, { result_conflicts: [...conflicts, { result_digest: node.resultDigest, result: node.result }] });
    return true;
  }
  put(node: Node): void {
    if (!this.#applyPut(node)) return;
    // Python stores only settled calls. Keep native pending state in memory until
    // settlement, while toBytes() can still materialize a recovery snapshot.
    if (node.meta.state === 'pending' && node.resultText === null && !this.#persisted.has(node.id)) return;
    this.#append(nodeRecord(node)); this.#persisted.add(node.id);
  }
  get(id: string): Node { return this.#store.get(id); }
  exists(id: string): boolean { return this.#store.exists(id); }
  children(id: string): string[] { return this.#store.children(id); }
  walk(rootId: string): Iterable<Node> { return this.#store.walk(rootId); }
  roots(): string[] { return this.#store.roots(); }
  updateMeta(id: string, patch: JsonObject): void {
    this.#store.updateMeta(id, patch);
    const node = this.#store.get(id);
    if (this.#persisted.has(id)) this.#append({ op: 'meta', id, patch });
    else if (node.meta.state !== 'pending') { this.#append(nodeRecord(node)); this.#persisted.add(id); }
  }
  finalize(node: Node): void {
    if (this.#persisted.has(node.id)) throw new IntegrityError('cannot finalize a pending dispatch restored from a log');
    this.#store.finalize(node);
    this.#append(nodeRecord(node)); this.#persisted.add(node.id);
  }
  toBytes(): Uint8Array {
    const chunks = this.#chunks.slice();
    for (const root of this.roots()) for (const node of this.walk(root)) if (!this.#persisted.has(node.id)) {
      if (chunks.length && chunks[chunks.length - 1].at(-1) !== 10) chunks.push(Buffer.from('\n'));
      chunks.push(Buffer.from(resultToText(nodeRecord(node)) + '\n', 'utf8'));
    }
    return Buffer.concat(chunks);
  }
  /** Exact hashrope PolynomialHash(base=131, prime=2^61-1); bigint avoids rounding. */
  contentHash(): bigint { return polynomialHash(this.toBytes()); }
  validateLog(): void {
    const replayed = new HashRopeStore(this.toBytes());
    if (canonicalText(replayed.roots()) !== canonicalText(this.roots())) throw new IntegrityError('hashrope log roots do not match store');
    for (const root of this.roots()) for (const node of this.walk(root)) {
      if (resultToText(replayed.get(node.id).toStorage() as unknown as JsonObject) !== resultToText(node.toStorage() as unknown as JsonObject)) throw new IntegrityError('hashrope log does not match store');
    }
  }
  transaction<T>(operation: () => T): T {
    const chunks = this.#chunks.slice(), persisted = new Set(this.#persisted);
    try { return this.#store.transaction(operation); }
    catch (error) { this.#chunks = chunks; this.#persisted = persisted; throw error; }
  }
  #rewrite(): void {
    this.#chunks = []; this.#persisted.clear();
    for (const root of this.roots()) for (const node of this.walk(root)) {
      if (node.meta.state === 'pending') continue;
      this.#append(nodeRecord(node)); this.#persisted.add(node.id);
    }
  }
  dropNodes(ids: ReadonlySet<string>): void { this.#store.dropNodes(ids); this.#rewrite(); }
  compact(): number { this.#rewrite(); return 0; }
}
