import { canonicalText, codePointCompare, JsonObject, JsonValue, resultToText, sha256, snapshot } from './identity.js';
import { IntegrityError, MissingNodeError, Node, StorageRecord } from './tree.js';
import { BudgetReservation, decimalAdd, decimalCompare, ReservationCheck, sameIdentity, WindowReservation } from './stores.js';

interface ReservationDetail { kind: 'budget' | 'window'; scopeId: string; meter: string; amount: string; windowSeconds?: number; }
interface RemoteReservation {
  requestDigest: string;
  state: 'active' | 'settled' | 'released';
  expiresAt: number;
  createdAt: number;
  completedAt: number | null;
  chargesDigest: string | null;
  details: ReservationDetail[];
}
interface WindowEvent { scopeId: string; meter: string; amount: string; settledAt: number; windowSeconds: number; }
/** npm-owned namespace; Python's backend-specific persisted schemas are separate. */
export interface RemoteState {
  version: 1;
  nodes: Record<string, StorageRecord>;
  owners: Record<string, string>;
  budget: Record<string, string>;
  reservations: Record<string, RemoteReservation>;
  windowEvents: Record<string, WindowEvent>;
}
export function createRemoteState(): RemoteState {
  return { version: 1, nodes: {}, owners: {}, budget: {}, reservations: {}, windowEvents: {} };
}
const MUTATIONS = new Set(['put', 'claim', 'updateMeta', 'finalize', 'pollardReserve', 'pollardSettle', 'pollardRelease', 'pollardRenew', 'dropNodes', 'compact', 'commitBatch']);
const READS = new Set(['get', 'exists', 'children', 'walk', 'roots', 'snapshot']);
export function isRemoteMutation(method: string): boolean {
  if (!MUTATIONS.has(method) && !READS.has(method)) throw new TypeError(`unsupported remote store method: ${method}`);
  return MUTATIONS.has(method);
}
function object(value: unknown, label: string): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new IntegrityError(`${label} must be an object`);
}
function text(value: unknown, label: string): asserts value is string {
  if (typeof value !== 'string' || !value) throw new TypeError(`${label} must be a nonempty string`);
}
function finite(value: unknown, label: string, positive = false): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > Number.MAX_SAFE_INTEGER || (positive && value === 0)) throw new TypeError(`${label} must be a ${positive ? 'positive' : 'nonnegative'} finite safe number`);
}
function numericRecord(value: unknown, label: string): asserts value is Record<string, number> {
  object(value, label);
  for (const [key, amount] of Object.entries(value)) { text(key, 'meter'); finite(amount, label); }
}
function decimalAmount(value: unknown): asserts value is string {
  if (typeof value !== 'string' || decimalCompare(value, '0') < 0) throw new IntegrityError('invalid persisted meter amount');
}
function own<T>(record: Record<string, T>, key: string): T | undefined { return Object.hasOwn(record, key) ? record[key] : undefined; }
function set<T>(record: Record<string, T>, key: string, value: T): void {
  Object.defineProperty(record, key, { value, enumerable: true, configurable: true, writable: true });
}
const compound = (...parts: string[]): string => canonicalText(parts);
function validateState(value: RemoteState): void {
  object(value, 'remote store state');
  if (value.version !== 1) throw new IntegrityError(`unsupported remote store schema version: ${String(value.version)}`);
  for (const key of ['nodes', 'owners', 'budget', 'reservations', 'windowEvents'] as const) object(value[key], `remote ${key}`);
  for (const [id, stored] of Object.entries(value.nodes)) {
    const node = Node.fromStorage(stored);
    if (node.id !== id) throw new IntegrityError('remote node key differs from its identity');
    if (node.parent !== null && !Object.hasOwn(value.nodes, node.parent)) throw new IntegrityError('remote node parent is missing');
  }
  for (const [id, owner] of Object.entries(value.owners)) {
    text(owner, 'dispatch owner');
    const node = own(value.nodes, id);
    if (!node || node.result_text !== null || node.meta.state !== 'pending') throw new IntegrityError('invalid pending dispatch ownership');
  }
  for (const amount of Object.values(value.budget)) decimalAmount(amount);
  for (const reservation of Object.values(value.reservations)) {
    object(reservation, 'reservation');
    if (!['active', 'settled', 'released'].includes(reservation.state) || !/^[a-f0-9]{64}$/.test(reservation.requestDigest)) throw new IntegrityError('invalid reservation state');
    finite(reservation.expiresAt, 'reservation expiry'); finite(reservation.createdAt, 'reservation creation');
    if (reservation.completedAt !== null) finite(reservation.completedAt, 'reservation completion');
    if (reservation.chargesDigest !== null && !/^[a-f0-9]{64}$/.test(reservation.chargesDigest)) throw new IntegrityError('invalid settlement digest');
    if (!Array.isArray(reservation.details)) throw new IntegrityError('invalid reservation details');
    for (const detail of reservation.details) {
      object(detail, 'reservation detail'); text(detail.scopeId, 'scope id'); text(detail.meter, 'meter'); decimalAmount(detail.amount);
      if (detail.kind === 'window') finite(detail.windowSeconds, 'windowSeconds', true);
      else if (detail.kind !== 'budget') throw new IntegrityError('invalid reservation detail kind');
    }
  }
  for (const event of Object.values(value.windowEvents)) {
    object(event, 'window event'); text(event.scopeId, 'window scope'); text(event.meter, 'window meter'); decimalAmount(event.amount);
    finite(event.settledAt, 'window settlement time'); finite(event.windowSeconds, 'window duration', true);
  }
}
function getNode(state: RemoteState, id: unknown): Node {
  text(id, 'node id');
  const stored = own(state.nodes, id);
  if (!stored) throw new MissingNodeError(id);
  return Node.fromStorage(stored);
}
function children(state: RemoteState, id: string): string[] {
  return Object.values(state.nodes).filter(node => node.parent === id).sort((a, b) => codePointCompare(a.kind, b.kind) || codePointCompare(a.id, b.id)).map(node => node.id);
}
function walk(state: RemoteState, rootId: unknown): StorageRecord[] {
  text(rootId, 'root id');
  const pending = [rootId], seen = new Set<string>(), nodes: StorageRecord[] = [];
  while (pending.length) {
    const id = pending.pop()!;
    if (seen.has(id)) throw new IntegrityError('cycle in remote store traversal');
    seen.add(id); nodes.push(getNode(state, id).toStorage()); pending.push(...children(state, id).reverse());
  }
  return nodes;
}
function put(state: RemoteState, record: unknown, owner?: unknown): void {
  const node = Node.fromStorage(record as StorageRecord);
  if (node.parent !== null && !own(state.nodes, node.parent)) throw new MissingNodeError(node.parent);
  const stored = own(state.nodes, node.id);
  if (stored) {
    const existing = Node.fromStorage(stored);
    if (!sameIdentity(existing, node)) throw new IntegrityError('remote node id collision');
    if (node.resultText !== null && existing.resultText !== node.resultText) {
      const conflict: JsonObject = { result_digest: node.resultDigest, result: node.result };
      const prior = Array.isArray(existing.meta.result_conflicts) ? [...existing.meta.result_conflicts] : [];
      if (!prior.some(value => resultToText(value) === resultToText(conflict))) prior.push(conflict);
      set(state.nodes, node.id, { ...stored, meta: { ...stored.meta, result_conflicts: prior } });
    }
    return;
  }
  set(state.nodes, node.id, node.toStorage());
  if (owner !== undefined && node.resultText === null && node.meta.state === 'pending') { text(owner, 'dispatch owner'); set(state.owners, node.id, owner); }
}
function validateRequests(budgets: unknown, windows: unknown, leaseSeconds: unknown): asserts budgets is BudgetReservation[] {
  if (!Array.isArray(budgets) || !Array.isArray(windows)) throw new TypeError('reservation requests must be arrays');
  finite(leaseSeconds, 'leaseSeconds', true);
  const keys = new Set<string>();
  for (const request of budgets) {
    object(request, 'budget request'); text(request.scopeId, 'scopeId');
    numericRecord(request.limits, 'budget limits'); numericRecord(request.baseline, 'budget baseline'); numericRecord(request.estimates, 'budget estimates');
    for (const meter of Object.keys(request.limits)) {
      const key = compound('budget', request.scopeId, meter);
      if (keys.has(key)) throw new TypeError('duplicate budget scope and meter');
      keys.add(key);
    }
  }
  for (const request of windows) {
    object(request, 'window request'); text(request.ledgerKey, 'ledgerKey'); text(request.meter, 'meter');
    finite(request.limit, 'window limit'); finite(request.amount, 'window amount'); finite(request.windowSeconds, 'windowSeconds', true);
    const key = compound('window', request.ledgerKey, request.meter);
    if (keys.has(key)) throw new TypeError('duplicate window scope and meter');
    keys.add(key);
  }
}
function reserve(state: RemoteState, id: unknown, budgets: unknown, windowsInput: unknown, leaseSeconds: unknown, now: number): ReservationCheck {
  text(id, 'reservation id'); validateRequests(budgets, windowsInput, leaseSeconds);
  const windows = windowsInput as WindowReservation[], lease = leaseSeconds as number;
  const encoded = resultToText({ budgets: [...budgets].sort((a, b) => codePointCompare(a.scopeId, b.scopeId)), windows: [...windows].sort((a, b) => codePointCompare(a.ledgerKey, b.ledgerKey)), leaseSeconds: lease } as unknown as JsonValue);
  const requestDigest = sha256(encoded), existing = own(state.reservations, id);
  if (existing) {
    if (existing.requestDigest !== requestDigest) throw new IntegrityError(`reservation retry changed request: ${id}`);
    if (existing.state !== 'active') throw new IntegrityError(`reservation is already ${existing.state}: ${id}`);
    if (existing.expiresAt <= now) throw new IntegrityError(`reservation expired before retry: ${id}`);
    return { ok: true };
  }
  const active = Object.values(state.reservations).filter(record => record.state === 'active' && record.expiresAt > now);
  const activeAmount = (kind: string, scope: string, meter: string): string => active.flatMap(record => record.details).filter(detail => detail.kind === kind && detail.scopeId === scope && detail.meter === meter).reduce((total, detail) => decimalAdd(total, detail.amount), '0');
  const details: ReservationDetail[] = [];
  for (const request of [...budgets].sort((a, b) => codePointCompare(a.scopeId, b.scopeId))) {
    for (const meter of Object.keys(request.limits).sort(codePointCompare)) {
      if (meter === 'depth') continue;
      const key = compound(request.scopeId, meter), baseline = String(own(request.baseline, meter) ?? 0);
      let settled = own(state.budget, key) ?? '0';
      if (decimalCompare(baseline, settled) > 0) settled = baseline;
      set(state.budget, key, settled);
      const remaining = decimalAdd(decimalAdd(String(request.limits[meter]), settled, true), activeAmount('budget', request.scopeId, meter), true);
      const requested = own(request.estimates, meter) ?? 0;
      if (decimalCompare(String(requested), remaining) > 0) return { ok: false, reason: 'budget', meter, requested, remaining: Number(remaining) };
      details.push({ kind: 'budget', scopeId: request.scopeId, meter, amount: String(requested) });
    }
  }
  for (const [key, event] of Object.entries(state.windowEvents)) if (event.settledAt <= now - event.windowSeconds) delete state.windowEvents[key];
  for (const request of [...windows].sort((a, b) => codePointCompare(a.ledgerKey, b.ledgerKey))) {
    const settled = Object.values(state.windowEvents).filter(event => event.scopeId === request.ledgerKey && event.meter === request.meter && event.settledAt > now - request.windowSeconds).reduce((total, event) => decimalAdd(total, event.amount), '0');
    const remaining = decimalAdd(decimalAdd(String(request.limit), settled, true), activeAmount('window', request.ledgerKey, request.meter), true);
    if (decimalCompare(String(request.amount), remaining) > 0) return { ok: false, reason: 'window', meter: request.meter, requested: request.amount, remaining: Number(remaining), windowSeconds: request.windowSeconds };
    details.push({ kind: 'window', scopeId: request.ledgerKey, meter: request.meter, amount: String(request.amount), windowSeconds: request.windowSeconds });
  }
  set(state.reservations, id, { requestDigest, state: 'active', expiresAt: now + lease, createdAt: now, completedAt: null, chargesDigest: null, details });
  return { ok: true };
}
function settle(state: RemoteState, id: unknown, charges: unknown, now: number): void {
  text(id, 'reservation id'); numericRecord(charges, 'charges');
  const current = own(state.reservations, id), chargesDigest = sha256(resultToText(charges));
  if (!current) throw new IntegrityError(`unknown reservation: ${id}`);
  if (current.state === 'settled') {
    if (current.chargesDigest !== chargesDigest) throw new IntegrityError(`reservation retry used different charges: ${id}`);
    return;
  }
  if (current.state !== 'active') throw new IntegrityError(`reservation is already ${current.state}: ${id}`);
  current.details.forEach((detail, index) => {
    const actual = String(own(charges, detail.meter) ?? 0);
    if (detail.kind === 'budget') {
      const key = compound(detail.scopeId, detail.meter), prior = own(state.budget, key);
      if (prior === undefined) throw new IntegrityError('budget state missing during settlement');
      set(state.budget, key, decimalAdd(prior, actual));
    } else if (decimalCompare(actual, '0') !== 0) {
      set(state.windowEvents, compound(id, String(index)), { scopeId: detail.scopeId, meter: detail.meter, amount: actual, settledAt: now, windowSeconds: detail.windowSeconds! });
    }
  });
  current.state = 'settled'; current.chargesDigest = chargesDigest; current.completedAt = now;
}

/** Called inside one backend transaction with that backend's authoritative time. */
export function applyRemoteOperation(original: RemoteState, method: string, args: unknown[], nowSeconds: number): { state: RemoteState; result: unknown } {
  const mutation = isRemoteMutation(method);
  finite(nowSeconds, 'backend time');
  // Work only on detached JSON snapshots, including reads. No failed operation
  // can leave the transaction adapter's previous state partially mutated.
  let state = snapshot(original as unknown as JsonValue) as unknown as RemoteState;
  validateState(state);
  let result: unknown;
  switch (method) {
    case 'snapshot': result = state; break;
    case 'commitBatch': {
      if (args[0] !== sha256(resultToText(state as unknown as JsonValue))) throw new IntegrityError('remote transaction snapshot changed before commit');
      if (!Array.isArray(args[1])) throw new TypeError('remote transaction operations must be an array');
      const results: unknown[] = [];
      for (const operation of args[1]) {
        object(operation, 'remote transaction operation');
        if (typeof operation.method !== 'string' || !['put', 'updateMeta', 'dropNodes', 'compact'].includes(operation.method) || !Array.isArray(operation.args)) throw new TypeError('remote transactions accept only offline store mutations');
        const applied = applyRemoteOperation(state, operation.method, operation.args, nowSeconds);
        state = applied.state; results.push(applied.result);
      }
      result = results; break;
    }
    case 'get': result = getNode(state, args[0]).toStorage(); break;
    case 'exists': text(args[0], 'node id'); result = Object.hasOwn(state.nodes, args[0]); break;
    case 'children': text(args[0], 'node id'); result = children(state, args[0]); break;
    case 'walk': result = walk(state, args[0]); break;
    case 'roots': result = Object.values(state.nodes).filter(node => node.parent === null).sort((a, b) => codePointCompare(String(a.payload.run ?? ''), String(b.payload.run ?? '')) || codePointCompare(a.id, b.id)).map(node => node.id); break;
    case 'put': put(state, args[0], args[1]); break;
    case 'claim': {
      const node = Node.fromStorage(args[0] as StorageRecord); text(args[1], 'dispatch owner');
      if (node.resultText !== null || node.meta.state !== 'pending') throw new IntegrityError('dispatch claim requires a pending node');
      result = !Object.hasOwn(state.nodes, node.id);
      if (result) put(state, node.toStorage(), args[1]);
      break;
    }
    case 'finalize': {
      const node = Node.fromStorage(args[0] as StorageRecord), old = getNode(state, node.id); text(args[1], 'dispatch owner');
      if (own(state.owners, node.id) !== args[1] || old.resultText !== null || old.meta.state !== 'pending' || node.meta.state !== 'completed' || !sameIdentity(old, node)) throw new IntegrityError('only the owner of a pending dispatch can finalize it once');
      set(state.nodes, node.id, node.toStorage()); delete state.owners[node.id]; break;
    }
    case 'updateMeta': {
      const node = getNode(state, args[0]); object(args[1], 'metadata patch');
      set(state.nodes, node.id, { ...node.toStorage(), meta: { ...node.meta, ...args[1] as JsonObject } });
      if (Object.hasOwn(args[1], 'state') && args[1].state !== 'pending') delete state.owners[node.id];
      break;
    }
    case 'pollardReserve': result = reserve(state, args[0], args[1], args[2], args[3], nowSeconds); break;
    case 'pollardSettle': settle(state, args[0], args[1], nowSeconds); break;
    case 'pollardRelease': {
      text(args[0], 'reservation id'); const reservation = own(state.reservations, args[0]);
      if (reservation && reservation.state !== 'released') {
        if (reservation.state !== 'active') throw new IntegrityError(`reservation is already ${reservation.state}: ${args[0]}`);
        reservation.state = 'released'; reservation.completedAt = nowSeconds;
      }
      break;
    }
    case 'pollardRenew': {
      text(args[0], 'reservation id'); finite(args[1], 'leaseSeconds', true);
      const reservation = own(state.reservations, args[0]);
      result = !!reservation && reservation.state === 'active' && reservation.expiresAt > nowSeconds;
      if (result) reservation!.expiresAt = nowSeconds + args[1];
      break;
    }
    case 'dropNodes': {
      if (!Array.isArray(args[0]) || !args[0].every(id => typeof id === 'string')) throw new TypeError('node ids must be an array of strings');
      const ids = new Set(args[0] as string[]);
      for (const node of Object.values(state.nodes)) if (!ids.has(node.id) && node.parent !== null && ids.has(node.parent)) throw new IntegrityError('cannot remove a parent while retaining its children');
      for (const id of ids) { delete state.nodes[id]; delete state.owners[id]; } break;
    }
    case 'compact':
      for (const [key, event] of Object.entries(state.windowEvents)) if (event.settledAt <= nowSeconds - event.windowSeconds) delete state.windowEvents[key];
      // Reservation tombstones must survive compaction: retries may arrive years later.
      result = 0; break;
  }
  if (mutation) validateState(state);
  return { state, result };
}
