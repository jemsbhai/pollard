import { randomUUID } from 'node:crypto';
import { RemoteRPC } from './remote-rpc.cjs';
import type { RemoteBackendOptions } from './remote-backends.cjs';
import { JsonObject, resultToText, sha256, snapshot } from './identity.js';
import { IntegrityError, MissingNodeError, Node, RecordingStore, StorageRecord, validateNode } from './tree.js';
import type { BudgetReservation, ReservationCheck, WindowReservation } from './stores.js';
import { applyRemoteOperation, isRemoteMutation, type RemoteState } from './remote-state.js';

export interface RemoteStoreOptions {
  storeId?: string;
  create?: boolean;
  timeoutMs?: number;
}
export interface RedisStoreOptions extends RemoteStoreOptions { prefix?: string; }
export interface MongoStoreOptions extends RemoteStoreOptions { database?: string; prefix?: string; }
export interface Neo4jStoreOptions extends RemoteStoreOptions { username: string; password: string; database?: string; }
export interface KafkaStoreOptions extends RemoteStoreOptions { topic: string; brokers: string[]; readOnly?: boolean; [key: string]: unknown; }

function remoteError(error: unknown): unknown {
  if (!(error instanceof Error)) return error;
  const constructors: Record<string, new (message: string) => Error> = { IntegrityError, MissingNodeError, TypeError, RangeError };
  const Constructor = Object.hasOwn(constructors, error.name) ? constructors[error.name] : undefined;
  return Constructor ? Object.assign(new Constructor(error.message), error) : error;
}

/** Synchronous Store facade; all asynchronous I/O runs in one dedicated Node worker. */
export class RemoteStore implements RecordingStore {
  readonly storeId: string;
  readonly #rpc: RemoteRPC;
  readonly #owner = randomUUID();
  constructor(config: RemoteBackendOptions) {
    if (!config.storeId || typeof config.storeId !== 'string') throw new TypeError('storeId must be a nonempty string');
    if (typeof config.create !== 'boolean') throw new TypeError('create must be boolean');
    if (config.timeoutMs !== undefined && (!Number.isFinite(config.timeoutMs) || config.timeoutMs <= 0)) throw new TypeError('timeoutMs must be positive');
    this.storeId = config.storeId;
    try { this.#rpc = new RemoteRPC(config); }
    catch (error) { throw remoteError(error); }
  }
  protected invoke(method: string, args: unknown[] = []): unknown {
    try { return this.#rpc.call(method, args); }
    catch (error) { throw remoteError(error); }
  }
  put(node: Node): void { validateNode(node); this.invoke('put', [node.toStorage(), this.#owner]); }
  claim(node: Node): boolean { validateNode(node); return this.invoke('claim', [node.toStorage(), this.#owner]) as boolean; }
  finalize(node: Node): void { validateNode(node); this.invoke('finalize', [node.toStorage(), this.#owner]); }
  get(id: string): Node { return Node.fromStorage(this.invoke('get', [id]) as StorageRecord); }
  exists(id: string): boolean { return this.invoke('exists', [id]) as boolean; }
  children(id: string): string[] { return this.invoke('children', [id]) as string[]; }
  updateMeta(id: string, patch: JsonObject): void { this.invoke('updateMeta', [id, snapshot(patch)]); }
  *walk(rootId: string): Iterable<Node> { for (const record of this.invoke('walk', [rootId]) as StorageRecord[]) yield Node.fromStorage(record); }
  roots(): string[] { return this.invoke('roots') as string[]; }
  close(): void { this.#rpc.close(); }
}

export class TransactionalRemoteStore extends RemoteStore {
  #staged?: { state: RemoteState; digest: string; operations: { method: string; args: unknown[] }[] };
  protected override invoke(method: string, args: unknown[] = []): unknown {
    if (!this.#staged) return super.invoke(method, args);
    const mutation = isRemoteMutation(method);
    if (mutation && !['put', 'updateMeta', 'dropNodes', 'compact'].includes(method)) throw new TypeError('remote maintenance transactions cannot dispatch calls or change reservations');
    const applied = applyRemoteOperation(this.#staged.state, method, args, Date.now() / 1000);
    this.#staged.state = applied.state;
    if (mutation) this.#staged.operations.push({ method, args: structuredClone(args) });
    return applied.result;
  }
  /** Optimistic maintenance transaction; a concurrent mutation aborts without retrying the callback. */
  transaction<T>(operation: () => T): T {
    const parent = this.#staged;
    const initial = parent ? parent.state : super.invoke('snapshot') as RemoteState;
    const staged = { state: initial, digest: parent?.digest ?? sha256(resultToText(initial as unknown as JsonObject)), operations: parent ? [...parent.operations] : [] };
    this.#staged = staged;
    try {
      const result = operation();
      if (result && typeof (result as { then?: unknown }).then === 'function') throw new TypeError('store transactions must be synchronous');
      if (parent) { parent.state = staged.state; parent.operations = staged.operations; }
      else if (staged.operations.length) super.invoke('commitBatch', [staged.digest, staged.operations]);
      return result;
    } finally { this.#staged = parent; }
  }
  dropNodes(ids: ReadonlySet<string>): void { this.invoke('dropNodes', [[...ids]]); }
  compact(): number { return this.invoke('compact') as number; }
  pollardReserve(id: string, budgets: BudgetReservation[], windows: WindowReservation[], leaseSeconds: number): ReservationCheck {
    return this.invoke('pollardReserve', [id, budgets, windows, leaseSeconds]) as ReservationCheck;
  }
  pollardSettle(id: string, charges: Record<string, number>): void { this.invoke('pollardSettle', [id, charges]); }
  pollardRelease(id: string): void { this.invoke('pollardRelease', [id]); }
  pollardRenew(id: string, leaseSeconds: number): boolean { return this.invoke('pollardRenew', [id, leaseSeconds]) as boolean; }
}
function options(backend: RemoteBackendOptions['backend'], url: string, config: RemoteStoreOptions): RemoteBackendOptions {
  if (typeof url !== 'string' || !url) throw new TypeError('backend URL must be a nonempty string');
  return { ...config, backend, url, storeId: config.storeId ?? 'default', create: config.create ?? true };
}
export class PostgresStore extends TransactionalRemoteStore {
  constructor(url: string, config: RemoteStoreOptions = {}) { super(options('postgres', url, config)); }
}
export class RedisStore extends TransactionalRemoteStore {
  constructor(url: string, config: RedisStoreOptions = {}) { super(options('redis', url, config)); }
}
export class MongoStore extends TransactionalRemoteStore {
  constructor(url: string, config: MongoStoreOptions = {}) { super(options('mongodb', url, config)); }
}
export class Neo4jStore extends TransactionalRemoteStore {
  constructor(url: string, config: Neo4jStoreOptions) {
    if (typeof config.username !== 'string' || typeof config.password !== 'string') throw new TypeError('Neo4j username and password must be strings');
    super(options('neo4j', url, config));
  }
}
/** Kafka supplies ordered Store operations, without shared budget arbitration. */
export class KafkaStore extends RemoteStore {
  constructor(config: KafkaStoreOptions) {
    if (!Array.isArray(config.brokers) || !config.brokers.length || config.brokers.some(broker => typeof broker !== 'string' || !broker)) throw new TypeError('Kafka brokers must be nonempty strings');
    if (typeof config.topic !== 'string' || !config.topic) throw new TypeError('Kafka topic must be a nonempty string');
    if (config.readOnly !== undefined && typeof config.readOnly !== 'boolean') throw new TypeError('readOnly must be boolean');
    if (config.readOnly && config.create === true) throw new TypeError('a read-only Kafka store cannot create a topic');
    super({ ...config, backend: 'kafka', storeId: config.storeId ?? 'default', create: config.create ?? !config.readOnly });
  }
}
