import { randomUUID } from 'node:crypto';
import { canonicalText, deepFreeze, digestPayload, IdentityPayload, JsonObject, nonnegativeInteger, objectPayload, snapshot } from './identity.js';
import { ActionSpec, Policy, Registry } from './registry.js';
import { BudgetExceeded, ConcurrentCallError, ConfirmationRequired, DuplicateRecordingError, IntegrityError, MemoryStore, MissingNodeError, Node, NodeKind, PolicyViolation, RecordingStore, Store, validateNode, verify } from './tree.js';
import { addCharges, chargeAmount, Charges, compatibleUsage, DepthMeter, Measurement, Meter, MeterPrecheckRefusal, StepMeter, TokenMeter, WallClockMeter, WindowMeter } from './meters.js';
import { AsyncDeltaCallback, AsyncStepResult, consumeStepResult, consumeStepResultAsync, DeltaCallback, PostDispatchOutcomeUnknown, recordedChunks, StepResult } from './streaming.js';
import { extractReplayContract, makeRevalidationPayload, NormalizedModelComparator, ReplayContract, RevalidationComparator, RevalidationComparison, RevalidationReport, REVALIDATION_FORMAT } from './revalidation.js';

export type ReplayMode = 'record' | 'hybrid' | 'replay';
export interface Budget { steps?: number; tokens?: number; depth?: number; usd?: number | string; seconds?: number; extra?: Record<string, number>; }
export interface CallOptions { attempt?: number; tokenEstimate?: number; onDelta?: AsyncDeltaCallback; keepChunks?: boolean; signal?: AbortSignal; }
export interface ToolCallOptions extends CallOptions { version?: string; }
export type StepFn = (payload: IdentityPayload) => StepResult;
export type AsyncStepFn = (payload: IdentityPayload) => AsyncStepResult | Promise<AsyncStepResult>;
export interface RevalidationOptions extends CallOptions { contract: ReplayContract; comparator?: RevalidationComparator; livePayload?: IdentityPayload; observationId?: string; }
export interface RuntimeOptions {
  store?: Store;
  mode?: ReplayMode;
  registry?: Registry;
  policies?: readonly Policy[];
  dryRun?: boolean;
  estimateTokens?: (payload: IdentityPayload) => number;
  reservedOutputTokens?: number;
  meters?: readonly Meter[];
  onNode?: (node: Node) => void;
  reservationLeaseSeconds?: number;
}
interface Scope { budget: Budget; anchorId: string; }
type Counters = Charges;
function chargeAt(values: Charges, name: string): number | undefined { return Object.hasOwn(values, name) ? values[name] : undefined; }
interface Reservation { estimates: Charges; approximate: Set<string>; id?: string; }
interface Arbiter extends Store {
  pollardReserve(id: string, budgets: {scopeId: string; limits: Charges; baseline: Charges; estimates: Charges}[], windows: {ledgerKey: string; meter: string; limit: number; amount: number; windowSeconds: number}[], seconds: number): {ok: boolean; meter?: string; requested?: number; remaining?: number; reason?: string; windowSeconds?: number};
  pollardSettle(id: string, charges: Charges): void;
  pollardRelease(id: string): void;
  pollardRenew?(id: string, seconds: number): boolean;
}
interface Begun { pending: Node; reservation: Reservation; recording: RecordingStore; start: number; stopLease: () => string | null; settled: boolean; measurements: Measurement[]; measurementsStopped: boolean; }
interface PreparedRevalidation { recorded: Node; livePayload: IdentityPayload; marked: IdentityPayload; observationId: string; comparator: RevalidationComparator; liveContract: IdentityPayload; recordedContract: IdentityPayload | null; }
interface PendingTool { parentId: string; payload: IdentityPayload; args: IdentityPayload; spec: ActionSpec; options: ToolCallOptions; }
// ESM and CommonJS consumers can share one Store, so both module graphs must use
// the same operation lock within this process realm.
const lockKey = Symbol.for('pollardai/v1:busy-stores');
const realm = globalThis as unknown as Record<symbol, unknown>;
const existingLocks = realm[lockKey];
if (existingLocks !== undefined && !(existingLocks instanceof WeakSet)) throw new TypeError('invalid Pollard store lock registry');
const busyStores = (existingLocks as WeakSet<Store> | undefined) ?? new WeakSet<Store>();
if (existingLocks === undefined) Object.defineProperty(globalThis, lockKey, { value: busyStores });

function budgetCopy(value: Budget): Budget {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('budget must be an object');
  const clean = snapshot(value as JsonObject) as Budget;
  for (const key of Object.keys(clean)) if (!['steps', 'tokens', 'depth', 'usd', 'seconds', 'extra'].includes(key)) throw new TypeError(`unsupported budget: ${key}; put custom limits in extra`);
  budgetLimits(clean);
  return deepFreeze(clean);
}
function budgetLimits(value: Budget): Charges {
  const limits: Charges = {};
  for (const [key, amount] of Object.entries(value)) if (key !== 'extra') {
    const parsed = key === 'usd' && typeof amount === 'string' && amount.trim() !== '' ? Number(amount) : amount;
    limits[key] = chargeAmount(parsed, `${key} budget`);
    if (key === 'depth') nonnegativeInteger(parsed, 'depth budget');
  }
  if (value.extra !== undefined) {
    if (!value.extra || typeof value.extra !== 'object' || Array.isArray(value.extra)) throw new TypeError('extra budget must be an object');
    for (const [key, amount] of Object.entries(value.extra)) { if (!key) throw new TypeError('custom meter name must be nonempty'); Object.defineProperty(limits, key, {value: chargeAmount(amount, `${key} budget`), enumerable: true, writable: true, configurable: true}); }
  }
  return limits;
}
function optionsCopy(value: ToolCallOptions): ToolCallOptions {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('call options must be an object');
  for (const key of Object.getOwnPropertyNames(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)!;
    if (!descriptor.enumerable || !('value' in descriptor)) throw new TypeError('call options cannot contain an accessor');
    if (!['attempt', 'tokenEstimate', 'version', 'onDelta', 'keepChunks', 'signal'].includes(key)) throw new TypeError(`unsupported call option: ${key}`);
  }
  if (value.attempt !== undefined) nonnegativeInteger(value.attempt, 'attempt');
  if (value.tokenEstimate !== undefined) nonnegativeInteger(value.tokenEstimate, 'tokenEstimate');
  if (value.version !== undefined && (typeof value.version !== 'string' || !value.version)) throw new TypeError('version must be a nonempty string');
  if (value.onDelta !== undefined && typeof value.onDelta !== 'function') throw new TypeError('onDelta must be a function');
  if (value.keepChunks !== undefined && typeof value.keepChunks !== 'boolean') throw new TypeError('keepChunks must be boolean');
  if (value.signal !== undefined && !(value.signal instanceof AbortSignal)) throw new TypeError('signal must be an AbortSignal');
  return Object.freeze({ ...value });
}
function counters(store: Store, anchorId: string, excludeReservedPending = false): Counters {
  const total: Charges = {};
  for (const node of store.walk(anchorId)) {
    validateNode(node);
    if (excludeReservedPending && node.meta.state === 'pending' && typeof node.meta.reservation_id === 'string') continue;
    const charges = node.meta.charges;
    if (charges === undefined) continue;
    if (!charges || typeof charges !== 'object' || Array.isArray(charges)) throw new IntegrityError('invalid stored charges');
    for (const [meter, amount] of Object.entries(charges)) Object.defineProperty(total, meter, { value: addCharges(Object.hasOwn(total, meter) ? total[meter] : 0, chargeAmount(amount, `stored ${meter} charge`)), enumerable: true, writable: true, configurable: true });
  }
  return total;
}
function checkFunction(fn: unknown, asyncAllowed: boolean): asserts fn is AsyncStepFn {
  if (typeof fn !== 'function') throw new TypeError('a handler function is required');
  if (!asyncAllowed && fn.constructor.name === 'AsyncFunction') throw new TypeError('use the async call method with an async handler');
}
function isPromise(value: unknown): value is PromiseLike<unknown> {
  return !!value && (typeof value === 'object' || typeof value === 'function') && typeof (value as PromiseLike<unknown>).then === 'function';
}

export class Runtime {
  readonly store: Store;
  readonly mode: ReplayMode;
  readonly registry?: Registry;
  readonly dryRun: boolean;
  readonly reservedOutputTokens: number;
  readonly estimateTokens?: (payload: IdentityPayload) => number;
  readonly meters: readonly Meter[];
  readonly onNode?: (node: Node) => void;
  readonly reservationLeaseSeconds: number;
  readonly policies: readonly ((context: Parameters<Policy['decide']>[0]) => ReturnType<Policy['decide']>)[];
  constructor(options: RuntimeOptions = {}) {
    const mode = options.mode ?? 'record';
    if (!['record', 'hybrid', 'replay'].includes(mode)) throw new TypeError('mode must be record, hybrid, or replay');
    if (options.dryRun !== undefined && typeof options.dryRun !== 'boolean') throw new TypeError('dryRun must be boolean');
    if (options.estimateTokens !== undefined && typeof options.estimateTokens !== 'function') throw new TypeError('estimateTokens must be a function');
    this.store = options.store ?? new MemoryStore();
    for (const name of ['put', 'get', 'exists', 'children', 'updateMeta', 'walk', 'roots'] as const) if (typeof this.store[name] !== 'function') throw new TypeError(`store lacks ${name}`);
    this.mode = mode;
    this.registry = options.registry;
    if (this.registry !== undefined && !(this.registry instanceof Registry)) throw new TypeError('registry must be a Registry');
    this.dryRun = options.dryRun ?? false;
    this.reservedOutputTokens = nonnegativeInteger(options.reservedOutputTokens ?? 0, 'reservedOutputTokens');
    this.estimateTokens = options.estimateTokens;
    this.meters = Object.freeze([...(options.meters ?? [new StepMeter(), new DepthMeter(), new WallClockMeter(), new TokenMeter({ estimator: options.estimateTokens, reservedOutputTokens: this.reservedOutputTokens })])]);
    const names = new Set<string>();
    for (const meter of this.meters) {
      if (!meter || typeof meter.name !== 'string' || !meter.name || typeof meter.charge !== 'function' || typeof meter.precheckEstimate !== 'function') throw new TypeError('meters require name, charge and precheckEstimate');
      if (names.has(meter.name)) throw new TypeError(`duplicate meter: ${meter.name}`); names.add(meter.name);
    }
    this.onNode = options.onNode;
    if (this.onNode !== undefined && typeof this.onNode !== 'function') throw new TypeError('onNode must be a function');
    this.reservationLeaseSeconds = chargeAmount(options.reservationLeaseSeconds ?? 60, 'reservationLeaseSeconds');
    if (!this.reservationLeaseSeconds) throw new TypeError('reservationLeaseSeconds must be positive');
    this.policies = Object.freeze((options.policies ?? []).map(policy => {
      if (typeof policy.decide !== 'function') throw new TypeError('policy must have a decide function');
      return policy.decide.bind(policy);
    }));
    Object.freeze(this);
  }
  /** Start at the root. Existing execution spending is retained across new Run handles. */
  run(label: string, options: { budget?: Budget; attempt?: number } = {}): Run {
    if (busyStores.has(this.store)) throw new ConcurrentCallError('store already has an active dispatch');
    if (typeof label !== 'string') throw new TypeError('run label must be a string');
    const budget = options.budget === undefined ? undefined : budgetCopy(options.budget);
    const root = Node.make({ kind: 'root', parent: null, payload: { run: label }, attempt: options.attempt ?? 0 });
    if (!this.store.exists(root.id)) {
      if (this.mode === 'replay') throw new MissingNodeError(`missing replay root: ${root.id}`);
      this.store.put(root);
      this.notify(this.store.get(root.id));
    }
    const stored = this.store.get(root.id);
    const report = verify(this.store, root.id);
    if (!report.ok) throw new IntegrityError(report.findings.map(f => f.message).join('; '));
    if (this.registry) {
      const bound = stored.meta.registry_digest;
      if (bound !== undefined && bound !== this.registry.registryDigest) throw new IntegrityError('run root is bound to a different registry');
      if (bound === undefined) {
        if (this.mode === 'replay') throw new IntegrityError('replay root has no registry binding');
        this.store.updateMeta(root.id, { registry_digest: this.registry.registryDigest });
      }
    }
    return new Run(this, root.id, root.id, label, budget === undefined ? [] : [{ budget, anchorId: root.id }]);
  }
  /** Observer failures never replace execution outcomes. */
  notify(node: Node): void {
    if (!this.onNode) return;
    const locked = busyStores.has(this.store); if (!locked) busyStores.add(this.store);
    try { const result: unknown = this.onNode(node); if (isPromise(result)) Promise.resolve(result).catch(() => undefined); } catch {}
    finally { if (!locked) busyStores.delete(this.store); }
  }
  resume(label: string, options: { budget?: Budget; attempt?: number } = {}): Run {
    const root = Node.make({ kind: 'root', parent: null, payload: { run: label }, attempt: options.attempt ?? 0 });
    if (!this.store.exists(root.id)) throw new MissingNodeError(`missing run root: ${root.id}`);
    const run = this.run(label, options); let best = root.id, bestDepth = 0;
    for (const node of this.store.walk(root.id)) {
      if (node.meta.pruned === true || this.store.children(node.id).some(id => this.store.get(id).meta.pruned !== true)) continue;
      let depth = 0, parent = node.parent;
      while (parent !== null) { depth++; parent = this.store.get(parent).parent; }
      if (depth > bestDepth || (depth === bestDepth && node.id < best)) { best = node.id; bestDepth = depth; }
    }
    return new Run(this, run.rootId, best, label, options.budget === undefined ? [] : [{ budget: options.budget, anchorId: root.id }]);
  }
}

export class Run {
  readonly #runtime: Runtime;
  readonly #rootId: string;
  readonly #label: string;
  #cursorId: string;
  readonly #scopes: Scope[];
  readonly #pending = new Map<string, PendingTool>();
  #avoided: Counters = {};
  /** Use Runtime.run() to construct a run. */
  constructor(runtime: Runtime, rootId: string, cursorId: string, label: string, scopes: Scope[]) {
    this.#runtime = runtime; this.#rootId = rootId; this.#cursorId = cursorId; this.#label = label;
    this.#scopes = scopes.map(scope => ({ budget: budgetCopy(scope.budget), anchorId: scope.anchorId }));
  }
  get rootId(): string { return this.#rootId; }
  get cursorId(): string { return this.#cursorId; }
  get label(): string { return this.#label; }
  get store(): Store { return this.#runtime.store; }
  get cursor(): Node { return this.store.get(this.#cursorId); }
  report(): { spent: Counters; avoided: Counters } { return { spent: counters(this.store, this.#rootId), avoided: { ...this.#avoided } }; }

  modelCall(payload: IdentityPayload, fn: StepFn, options: CallOptions = {}): Node {
    return this.#callSync('model_call', payload, fn, options);
  }
  modelCallAsync(payload: IdentityPayload, fn: AsyncStepFn, options: CallOptions = {}): Promise<Node> {
    return this.#callAsync('model_call', payload, fn, options);
  }
  revalidateModelCall(payload: IdentityPayload, fn: StepFn, options: RevalidationOptions): RevalidationReport {
    const prepared = this.#prepareRevalidation(payload, options);
    const { contract: _contract, comparator: _comparator, livePayload: _livePayload, observationId: _observationId, ...call } = options;
    const live = this.#callSync('model_call', prepared.marked, () => fn(prepared.livePayload), { ...call, attempt: 0 });
    return this.#finishRevalidation(prepared, live);
  }
  async revalidateModelCallAsync(payload: IdentityPayload, fn: AsyncStepFn, options: RevalidationOptions): Promise<RevalidationReport> {
    const prepared = this.#prepareRevalidation(payload, options);
    const { contract: _contract, comparator: _comparator, livePayload: _livePayload, observationId: _observationId, ...call } = options;
    const live = await this.#callAsync('model_call', prepared.marked, () => fn(prepared.livePayload), { ...call, attempt: 0 });
    return this.#finishRevalidation(prepared, live);
  }
  toolCall(name: string, args: IdentityPayload, fn?: StepFn, options: ToolCallOptions = {}): Node {
    this.#idle();
    if (!this.#runtime.registry) return this.#callSync('tool_call', { tool: name, args }, fn as StepFn, options);
    const prepared = this.#prepareTool(name, args, options);
    if ('recorded' in prepared) return this.#replaySync(prepared.recorded, options);
    if (this.#runtime.dryRun && prepared.spec.sideEffects) return this.#dryRun(prepared.payload, options);
    return this.#callSync('tool_call', prepared.payload, () => prepared.spec.handler!(prepared.args) as JsonObject, options, prepared.spec.handler);
  }
  async toolCallAsync(name: string, args: IdentityPayload, fn?: AsyncStepFn, options: ToolCallOptions = {}): Promise<Node> {
    this.#idle();
    if (!this.#runtime.registry) return this.#callAsync('tool_call', { tool: name, args }, fn as AsyncStepFn, options);
    const prepared = this.#prepareTool(name, args, options);
    if ('recorded' in prepared) return this.#replayAsync(prepared.recorded, options);
    if (this.#runtime.dryRun && prepared.spec.sideEffects) return this.#dryRun(prepared.payload, options);
    return this.#callAsync('tool_call', prepared.payload, () => prepared.spec.handler!(prepared.args), options);
  }
  confirm(token: string): Node {
    this.#idle();
    const pending = this.#confirmation(token);
    if (this.#runtime.dryRun && pending.spec.sideEffects) return this.#dryRun(pending.payload, pending.options);
    return this.#callSync('tool_call', pending.payload, () => pending.spec.handler!(pending.args) as JsonObject, pending.options, pending.spec.handler);
  }
  async confirmAsync(token: string): Promise<Node> {
    this.#idle();
    const pending = this.#confirmation(token);
    if (this.#runtime.dryRun && pending.spec.sideEffects) return this.#dryRun(pending.payload, pending.options);
    return this.#callAsync('tool_call', pending.payload, () => pending.spec.handler!(pending.args), pending.options);
  }
  note(payload: IdentityPayload, options: { attempt?: number } = {}): Node {
    this.#idle();
    const candidate = Node.make({ kind: 'note', parent: this.#cursorId, payload, attempt: options.attempt ?? 0 });
    if (this.#runtime.mode === 'replay') return this.#structuralReplay(candidate);
    busyStores.add(this.store);
    try { this.#precheck('note', candidate.payload, {}); this.#putObserved(candidate); this.#cursorId = candidate.id; }
    finally { busyStores.delete(this.store); }
    return this.store.get(candidate.id);
  }
  /** A branch keeps its own cursor and shares ancestor spending. */
  branch(options: { attempt?: number; budget?: Budget } = {}): Run {
    this.#idle();
    const budget = options.budget === undefined ? undefined : budgetCopy(options.budget);
    const candidate = Node.make({ kind: 'note', parent: this.#cursorId, payload: { branch: true }, attempt: options.attempt ?? 0 });
    if (this.#runtime.mode === 'replay') this.#structuralReplay(candidate, false);
    else { busyStores.add(this.store); try { this.#precheck('note', candidate.payload, {}); this.#putObserved(candidate); } finally { busyStores.delete(this.store); } }
    const scopes = [...this.#scopes];
    if (budget !== undefined) scopes.push({ budget, anchorId: candidate.id });
    return new Run(this.#runtime, this.#rootId, candidate.id, this.#label, scopes);
  }
  /** Rollback changes the cursor only; it never refunds recorded charges. */
  rollback(target?: string, steps = 1): Node {
    this.#idle();
    nonnegativeInteger(steps, 'rollback steps');
    let selected = target ?? this.#cursorId;
    if (target === undefined) for (let i = 0; i < steps; i++) {
      const parent = this.store.get(selected).parent;
      if (parent === null) break;
      selected = parent;
    }
    let ancestor: string | null = this.#cursorId;
    const seen = new Set<string>();
    while (ancestor !== null && ancestor !== selected) {
      if (seen.has(ancestor)) throw new IntegrityError('cycle in ancestry');
      seen.add(ancestor); ancestor = this.store.get(ancestor).parent;
    }
    if (ancestor === null) throw new TypeError('rollback target must be an ancestor of the cursor');
    this.#cursorId = selected;
    return this.cursor;
  }
  prune(): void { this.#idle(); if (this.#runtime.mode === 'replay') throw new TypeError('cannot prune in replay mode'); this.store.updateMeta(this.#cursorId, { pruned: true }); }

  #idle(): void { if (busyStores.has(this.store)) throw new ConcurrentCallError('store already has an active dispatch'); }
  #putObserved(node: Node): void { const fresh = !this.store.exists(node.id); this.store.put(node); if (fresh) this.#runtime.notify(this.store.get(node.id)); }
  #recorded(kind: NodeKind, payload: IdentityPayload, options: CallOptions): Node | null {
    const candidate = Node.make({ kind, parent: this.#cursorId, payload, attempt: options.attempt ?? 0 });
    if (!this.store.exists(candidate.id)) {
      if (this.#runtime.mode === 'replay') throw new MissingNodeError(`missing replay recording: ${candidate.id}`);
      return null;
    }
    if (this.#runtime.mode === 'record') throw new DuplicateRecordingError(`recording already exists: ${candidate.id}; use a new attempt or hybrid mode`);
    const node = this.store.get(candidate.id);
    const report = verify(this.store, candidate.id);
    if (!report.ok) throw new IntegrityError(report.findings.map(f => f.message).join('; '));
    if (node.resultText === null || node.meta.state === 'pending' || node.meta.state === 'failed' || node.meta.dry_run === true) throw new IntegrityError('recording has no completed executable result');
    const charges = node.meta.charges;
    if (charges && typeof charges === 'object' && !Array.isArray(charges)) for (const [meter, amount] of Object.entries(charges)) Object.defineProperty(this.#avoided, meter, { value: addCharges(Object.hasOwn(this.#avoided, meter) ? this.#avoided[meter] : 0, chargeAmount(amount, `stored ${meter} charge`)), enumerable: true, writable: true, configurable: true });
    this.#cursorId = node.id;
    return node;
  }
  #structuralReplay(candidate: Node, move = true): Node {
    if (!this.store.exists(candidate.id)) throw new MissingNodeError(`missing replay structure: ${candidate.id}`);
    const report = verify(this.store, candidate.id);
    if (!report.ok) throw new IntegrityError(report.findings.map(f => f.message).join('; '));
    const node = this.store.get(candidate.id);
    if (move) this.#cursorId = node.id;
    return node;
  }
  #depth(): number {
    let id: string | null = this.#cursorId;
    let depth = -1;
    const seen = new Set<string>();
    while (id !== null) {
      if (seen.has(id)) throw new IntegrityError('cycle in ancestry');
      seen.add(id); depth++; id = this.store.get(id).parent;
    }
    return depth + 1;
  }
  #precheck(kind: NodeKind, payload: IdentityPayload, options: CallOptions): Reservation {
    const call = kind === 'tool_call' || kind === 'model_call';
    const estimates: Charges = {}, approximate = new Set<string>();
    for (const meter of this.#runtime.meters) {
      let amount: number | null;
      try { amount = meter.precheckEstimate(kind, payload); }
      catch (error) {
        if (!(error instanceof MeterPrecheckRefusal)) throw error;
        this.#refuseBudget(kind, payload, meter.name, error.requested, error.remaining, { reason: error.reason, detail: error.detail }, error.auditMeta);
      }
      if (amount !== null) { Object.defineProperty(estimates, meter.name, { value: chargeAmount(amount, `estimate ${meter.name}`), enumerable: true, writable: true, configurable: true }); if (meter.precheckIsEstimate) approximate.add(meter.name); }
    }
    if (call && options.tokenEstimate !== undefined) { estimates.tokens = nonnegativeInteger(options.tokenEstimate + (kind === 'model_call' ? this.#runtime.reservedOutputTokens : 0), 'token estimate'); approximate.add('tokens'); }
    estimates.depth = this.#depth();
    for (const scope of this.#scopes) {
      const spent = counters(this.store, scope.anchorId);
      for (const [meter, limit] of Object.entries(budgetLimits(scope.budget))) {
        const remaining = limit - (meter === 'depth' ? 0 : (chargeAt(spent, meter) ?? 0));
        const estimate = chargeAt(estimates, meter) ?? 0;
        if (remaining < 0 || (remaining < estimate && addCharges(chargeAt(spent, meter) ?? 0, estimate) > limit)) this.#refuseBudget(kind, payload, meter, String(estimate), String(remaining), approximate.has(meter) ? { estimated: 'true' } : {});
      }
    }
    const reservation: Reservation = { estimates, approximate };
    if (!call) return reservation;
    const store = this.store as Arbiter;
    const windows = this.#runtime.meters.filter((meter): meter is WindowMeter => meter instanceof WindowMeter).map(meter => ({ ledgerKey: meter.ledgerKey(this.#rootId), meter: meter.name, limit: meter.limit, amount: chargeAt(estimates, meter.name) ?? 0, windowSeconds: meter.windowSeconds }));
    if (typeof store.pollardReserve === 'function') {
      const budgets = this.#scopes.map(scope => ({ scopeId: scope.anchorId, limits: budgetLimits(scope.budget), baseline: counters(store, scope.anchorId, true), estimates })).filter(request => Object.keys(request.limits).some(name => name !== 'depth'));
      if (budgets.length || windows.length) {
        const id = randomUUID();
        const check = store.pollardReserve(id, budgets, windows, this.#runtime.reservationLeaseSeconds);
        if (!check.ok) this.#refuseBudget(kind, payload, check.meter ?? 'unknown', String(check.requested ?? 0), String(check.remaining ?? 0), { reason: check.reason ?? 'budget', ...(check.windowSeconds === undefined ? {} : { window_seconds: String(check.windowSeconds) }), ...(approximate.has(check.meter ?? '') ? { estimated: 'true' } : {}) });
        reservation.id = id;
      }
    } else {
      // The process-wide dispatch lock makes a local MemoryStore window atomic.
      for (const window of windows) {
        let spent = 0; const cutoff = Date.now() - window.windowSeconds * 1000;
        for (const node of store.walk(this.#rootId)) {
          if (typeof node.meta.created_at !== 'string' || Date.parse(node.meta.created_at) <= cutoff) continue;
          const charges = node.meta.charges;
          if (charges && typeof charges === 'object' && !Array.isArray(charges) && Object.hasOwn(charges, window.meter)) spent = addCharges(spent, chargeAmount(charges[window.meter]));
        }
        if (addCharges(spent, window.amount) > window.limit) this.#refuseBudget(kind, payload, window.meter, String(window.amount), String(window.limit - spent), { reason: 'window', window_seconds: String(window.windowSeconds) });
      }
    }
    return reservation;
  }
  #refuseBudget(kind: NodeKind, payload: IdentityPayload, meter: string, requested?: string, remaining?: string, extra: IdentityPayload = {}, auditMeta: JsonObject = {}): never {
    const refusal = Node.make({ kind: 'refusal', parent: this.#cursorId, payload: { reason: 'budget', meter, ...(requested === undefined ? {} : { requested }), ...(remaining === undefined ? {} : { remaining }), blocked_kind: kind, blocked_payload_digest: digestPayload(payload), ...extra }, meta: { ...auditMeta, created_at: new Date().toISOString() } });
    this.#putObserved(refusal); this.#cursorId = refusal.id;
    throw new BudgetExceeded(typeof extra.detail === 'string' ? extra.detail : `budget exceeded for ${meter}`, refusal.id);
  }
  #refusePolicy(detail: string, payload: IdentityPayload): never {
    const refusalPayload: IdentityPayload = { reason: 'policy', detail, blocked_kind: 'tool_call', blocked_payload_digest: digestPayload(payload) };
    if (this.#runtime.registry) refusalPayload.registry_digest = this.#runtime.registry.registryDigest;
    const refusal = Node.make({ kind: 'refusal', parent: this.#cursorId, payload: refusalPayload });
    if (this.#runtime.mode === 'replay') this.#structuralReplay(refusal);
    else { this.#putObserved(refusal); this.#cursorId = refusal.id; }
    throw new PolicyViolation(detail, refusal.id);
  }
  #prepareTool(name: string, args: IdentityPayload, options: ToolCallOptions): PendingTool | { recorded: Node } {
    options = optionsCopy(options);
    if (typeof name !== 'string' || !name) throw new TypeError('tool name must be nonempty');
    objectPayload(args, 'args');
    const cleanArgs = deepFreeze(snapshot(args, true));
    const registry = this.#runtime.registry!;
    let spec: ActionSpec;
    try { spec = registry.get(name); } catch { this.#refusePolicy(`unknown registered action: ${name}`, { tool: name, args: cleanArgs }); }
    const auditArgs = spec.redactArgs(cleanArgs);
    if (options.version !== undefined && options.version !== spec.version) this.#refusePolicy(`unknown registered action: ${name}@${options.version}`, { tool: name, args: auditArgs });
    const finding = spec.validateArgs(cleanArgs);
    if (finding) this.#refusePolicy(`schema validation failed: ${finding}`, { tool: name, args: auditArgs });
    const payload = deepFreeze({ tool: spec.name, version: spec.version, args: auditArgs, spec_digest: spec.specDigest, registry_digest: registry.registryDigest });
    const recorded = this.#recorded('tool_call', payload, options);
    if (recorded) return { recorded };
    let confirmation = false;
    busyStores.add(this.store);
    try {
      for (const decide of this.#runtime.policies) {
        const decision = decide(Object.freeze({ spec, args: cleanArgs, cursorId: this.#cursorId, runLabel: this.#label, counters: Object.freeze(counters(this.store, this.#rootId)) }));
        if (isPromise(decision)) Promise.resolve(decision).catch(() => undefined);
        if (!['allow', 'deny', 'confirm'].includes(decision)) this.#refusePolicy('invalid policy decision', payload);
        if (decision === 'deny') this.#refusePolicy('denied by policy', payload);
        if (decision === 'confirm') confirmation = true;
      }
    } finally { busyStores.delete(this.store); }
    const prepared = { parentId: this.#cursorId, payload, args: cleanArgs, spec, options: { ...options } };
    if (confirmation) {
      const token = Node.make({ kind: 'tool_call', parent: this.#cursorId, payload, attempt: options.attempt ?? 0 }).id;
      this.#pending.set(token, prepared);
      throw new ConfirmationRequired(token);
    }
    if (!spec.handler && !(this.#runtime.dryRun && spec.sideEffects)) this.#refusePolicy('registered action has no handler', payload);
    return prepared;
  }
  #confirmation(token: string): PendingTool {
    const pending = this.#pending.get(token);
    if (!pending) throw new TypeError('unknown or consumed confirmation token');
    if (this.#cursorId !== pending.parentId) throw new TypeError('cannot confirm after cursor moved');
    this.#pending.delete(token);
    if (!pending.spec.handler && !(this.#runtime.dryRun && pending.spec.sideEffects)) this.#refusePolicy('registered action has no handler', pending.payload);
    return pending;
  }
  #dryRun(payload: IdentityPayload, options: CallOptions): Node {
    const recorded = this.#recorded('tool_call', payload, options);
    if (recorded) return recorded;
    busyStores.add(this.store);
    try {
      const reservation = this.#precheck('tool_call', payload, options);
      try {
        const node = Node.make({ kind: 'tool_call', parent: this.#cursorId, payload, attempt: options.attempt ?? 0, meta: { dry_run: true, charges: {}, created_at: new Date().toISOString() } });
        if (this.store.exists(node.id)) throw new DuplicateRecordingError(`recording appeared before dispatch: ${node.id}`);
        this.store.put(node); this.#cursorId = node.id; this.#runtime.notify(this.store.get(node.id));
        return this.store.get(node.id);
      } finally { if (reservation.id) (this.store as Arbiter).pollardRelease(reservation.id); }
    } finally { busyStores.delete(this.store); }
  }
  #begin(kind: NodeKind, payload: IdentityPayload, options: CallOptions): Begun {
    const store = this.store as RecordingStore;
    if (typeof store.finalize !== 'function') throw new TypeError('live execution requires the optional RecordingStore.finalize capability');
    options.signal?.throwIfAborted();
    const reservation = this.#precheck(kind, payload, options);
    const pending = Node.make({ kind, parent: this.#cursorId, payload, attempt: options.attempt ?? 0,
      meta: { state: 'pending', accounting_unknown: true, charges: this.#estimatedCharges(reservation), created_at: new Date().toISOString(), ...(reservation.id ? { reservation_id: reservation.id } : {}) } });
    // Callbacks may have changed trusted store state after the first lookup.
    // Never rely on an idempotent put as proof this dispatch owns the identity.
    try {
      options.signal?.throwIfAborted();
      if (store.exists(pending.id)) throw new DuplicateRecordingError(`recording appeared before dispatch: ${pending.id}`);
      const claiming = store as RecordingStore & { claim?(node: Node): boolean };
      if (claiming.claim) { if (!claiming.claim(pending)) throw new DuplicateRecordingError(`recording appeared before dispatch: ${pending.id}`); }
      else store.put(pending);
    } catch (error) { if (reservation.id) try { (store as unknown as Arbiter).pollardRelease(reservation.id); } catch (cleanup) { this.#attachCleanup(error, cleanup); } throw error; }
    let lost: string | null = null, timer: ReturnType<typeof setInterval> | undefined, stopped = false;
    const arbiter = store as unknown as Arbiter;
    if (reservation.id && arbiter.pollardRenew) {
      timer = setInterval(() => { try { if (!arbiter.pollardRenew!(reservation.id!, this.#runtime.reservationLeaseSeconds)) lost = 'renewal declined'; } catch { lost = 'renewal failed'; } }, Math.max(1, this.#runtime.reservationLeaseSeconds * 1000 / 3));
      timer.unref();
    }
    return { pending, reservation, recording: store, start: performance.now(), settled: false, measurements: [], measurementsStopped: false, stopLease: () => {
      if (stopped) return lost; stopped = true;
      if (timer) { clearInterval(timer); timer = undefined; }
      if (reservation.id && arbiter.pollardRenew) try { if (!arbiter.pollardRenew(reservation.id, this.#runtime.reservationLeaseSeconds)) lost = 'renewal declined'; } catch { lost = 'renewal failed'; }
      return lost;
    } };
  }
  #startMeasurements(begun: Begun): void {
    for (const meter of this.#runtime.meters) if (meter.measure) {
      const measurement = meter.measure();
      if (!measurement || typeof measurement.start !== 'function' || typeof measurement.stop !== 'function' || typeof measurement.readings !== 'function') throw new TypeError('measurement requires start, stop and readings methods');
      measurement.start(); begun.measurements.push(measurement);
    }
  }
  #stopMeasurements(begun: Begun): void {
    if (begun.measurementsStopped) return;
    begun.measurementsStopped = true; const errors: unknown[] = [];
    for (const measurement of [...begun.measurements].reverse()) try { measurement.stop(); } catch (error) { errors.push(error); }
    if (errors.length === 1) throw errors[0];
    if (errors.length) throw new AggregateError(errors, 'measurement cleanup failed');
  }
  #measurementReadings(begun: Begun): JsonObject {
    const readings: JsonObject = {};
    for (const measurement of begun.measurements) for (const [key, value] of Object.entries(snapshot(measurement.readings()))) Object.defineProperty(readings, key, { value, enumerable: true, writable: true, configurable: true });
    return readings;
  }
  #estimatedCharges(reservation: Reservation): Charges { return Object.fromEntries(Object.entries(reservation.estimates).filter(([key, value]) => key !== 'depth' && value !== 0)); }
  #attachCleanup(error: unknown, cleanup: unknown): void {
    if (error && typeof error === 'object') try { Object.defineProperty(error, 'cause', { value: cleanup, configurable: true }); } catch {}
  }
  #fail(begun: Begun, error: unknown): void {
    const lease = begun.stopLease();
    try { this.#stopMeasurements(begun); } catch (cleanup) { this.#attachCleanup(error, cleanup); }
    try {
      if (this.store.get(begun.pending.id).meta.state !== 'pending') return;
      const charges = this.#estimatedCharges(begun.reservation);
      let readings: JsonObject = {};
      try { readings = this.#measurementReadings(begun); } catch (cleanup) { this.#attachCleanup(error, cleanup); }
      this.store.updateMeta(begun.pending.id, { ...readings, state: 'failed', accounting_unknown: true, outcome: 'unknown', duration_s: (performance.now() - begun.start) / 1000, error_type: error instanceof Error ? error.name : typeof error, ...(lease ? { reservation_lease: { status: 'lost', detail: lease } } : {}) });
      if (begun.reservation.id && !begun.settled) { (this.store as Arbiter).pollardSettle(begun.reservation.id, charges); begun.settled = true; }
      this.#runtime.notify(this.store.get(begun.pending.id));
    } catch (cleanup) { this.#attachCleanup(error, cleanup); }
  }
  #finish(begun: Begun, result: JsonObject): Node {
    const { pending, reservation, recording } = begun;
    this.#stopMeasurements(begun);
    const copied = snapshot(result);
    const meta: JsonObject = { ...this.#measurementReadings(begun), state: 'completed', accounting_unknown: false, created_at: pending.meta.created_at, duration_s: (performance.now() - begun.start) / 1000 };
    const charges: Charges = {}, fallbacks: JsonObject = {};
    for (const meter of this.#runtime.meters) {
      let amount = chargeAmount(meter.charge(pending.kind, pending.payload, copied, meta), `charge ${meter.name}`);
      const estimate = chargeAt(reservation.estimates, meter.name);
      if (amount === 0 && estimate !== undefined && reservation.approximate.has(meter.name)) {
        let reason: string | null = compatibleUsage(copied) ? null : 'missing_or_invalid_provider_usage';
        if (!reason && meter.precheckFallbackReason) try { const candidate = meter.precheckFallbackReason(pending.kind, pending.payload, copied, meta); if (typeof candidate === 'string' && candidate) reason = candidate; } catch {}
        if (reason) { amount = estimate; Object.defineProperty(fallbacks, meter.name, { value: { reason, source: 'precheck_estimate' }, enumerable: true, writable: true, configurable: true }); }
      }
      if (amount !== 0) Object.defineProperty(charges, meter.name, { value: amount, enumerable: true, writable: true, configurable: true });
    }
    meta.charges = charges;
    if (Object.keys(fallbacks).length) meta.accounting_fallbacks = fallbacks;
    if (copied.usage && typeof copied.usage === 'object' && !Array.isArray(copied.usage)) meta.usage = copied.usage;
    const lease = begun.stopLease();
    if (lease) meta.reservation_lease = { status: 'lost', detail: lease };
    if (reservation.id) {
      meta.reservation_id = reservation.id; begun.settled = true;
      try { (this.store as Arbiter).pollardSettle(reservation.id, charges); }
      catch (error) {
        // Provider success remains inspectable even when ledger persistence is uncertain.
        meta.settlement = { status: 'uncertain', error_type: error instanceof Error ? error.name : typeof error };
        try {
          const observed = Node.make({ kind: pending.kind, parent: pending.parent, payload: pending.payload, attempt: pending.attempt, result: copied, meta });
          recording.finalize(observed); this.#cursorId = observed.id; this.#runtime.notify(this.store.get(observed.id));
        } catch (cleanup) { this.#attachCleanup(error, cleanup); }
        throw error;
      }
    }
    const complete = Node.make({ kind: pending.kind, parent: pending.parent, payload: pending.payload, attempt: pending.attempt, result: copied, meta });
    recording.finalize(complete); this.#cursorId = complete.id;
    this.#runtime.notify(this.store.get(complete.id));
    if (lease) throw new ReservationLeaseLost('shared reservation lease was lost while the call was running', reservation.id ?? '', complete.id, lease);
    return this.store.get(complete.id);
  }
  #callSync(kind: NodeKind, payload: IdentityPayload, fn: StepFn, options: CallOptions, originalFn?: unknown): Node {
    this.#idle();
    options = optionsCopy(options);
    objectPayload(payload);
    const clean = deepFreeze(snapshot(payload, true));
    const recorded = this.#recorded(kind, clean, options);
    if (recorded) return this.#replaySync(recorded, options);
    checkFunction(originalFn ?? fn, false);
    busyStores.add(this.store);
    let begun: Begun | undefined;
    let held = false;
    try {
      begun = this.#begin(kind, clean, options);
      this.#startMeasurements(begun);
      const result = fn(clean);
      if (isPromise(result)) {
        held = true;
        Promise.resolve(result).catch(() => undefined).finally(() => busyStores.delete(this.store));
        throw new TypeError('handler returned a promise; use an async call method');
      }
      return this.#finish(begun, consumeStepResult(result, { ...options, onDelta: options.onDelta as DeltaCallback }));
    } catch (error) {
      const original = error instanceof PostDispatchOutcomeUnknown ? error.error : error;
      if (begun) this.#fail(begun, original);
      throw original;
    } finally { if (!held) busyStores.delete(this.store); }
  }
  async #callAsync(kind: NodeKind, payload: IdentityPayload, fn: AsyncStepFn, options: CallOptions): Promise<Node> {
    this.#idle();
    options = optionsCopy(options);
    objectPayload(payload);
    const clean = deepFreeze(snapshot(payload, true));
    const recorded = this.#recorded(kind, clean, options);
    if (recorded) return this.#replayAsync(recorded, options);
    checkFunction(fn, true);
    busyStores.add(this.store);
    let begun: Begun | undefined, held = false;
    let removeAbort = () => {};
    try {
      begun = this.#begin(kind, clean, options);
      this.#startMeasurements(begun);
      const operation = Promise.resolve(fn(clean)).then(result => consumeStepResultAsync(result, options));
      let result: JsonObject;
      if (options.signal) {
        const signal = options.signal;
        const abort = new Promise<never>((_resolve, reject) => {
          const onAbort = () => { held = true; operation.catch(() => undefined).finally(() => busyStores.delete(this.store)); reject(signal.reason); };
          signal.addEventListener('abort', onAbort, { once: true }); removeAbort = () => signal.removeEventListener('abort', onAbort);
          if (signal.aborted) onAbort();
        });
        result = await Promise.race([operation, abort]);
      } else result = await operation;
      return this.#finish(begun, result);
    } catch (error) {
      const original = error instanceof PostDispatchOutcomeUnknown ? error.error : error;
      if (begun) this.#fail(begun, original);
      throw original;
    } finally { removeAbort(); if (!held) busyStores.delete(this.store); }
  }
  #replaySync(node: Node, options: CallOptions): Node {
    if (!options.onDelta) return node;
    busyStores.add(this.store);
    try { for (const chunk of recordedChunks(node.result)) { const result = options.onDelta(chunk); if (isPromise(result)) { Promise.resolve(result).catch(() => undefined); throw new TypeError('async onDelta requires an async call method'); } } return node; }
    finally { busyStores.delete(this.store); }
  }
  async #replayAsync(node: Node, options: CallOptions): Promise<Node> {
    if (!options.onDelta) return node;
    busyStores.add(this.store);
    try { for (const chunk of recordedChunks(node.result)) { options.signal?.throwIfAborted(); await options.onDelta(chunk); } return node; }
    finally { busyStores.delete(this.store); }
  }
  #prepareRevalidation(payload: IdentityPayload, options: RevalidationOptions): PreparedRevalidation {
    this.#idle();
    if (this.#runtime.mode !== 'record') throw new TypeError('live revalidation requires record mode');
    if (this.#runtime.dryRun) throw new TypeError('live revalidation is unavailable in dry-run mode');
    if (!(options.contract instanceof ReplayContract)) throw new TypeError('contract must be a ReplayContract');
    const comparator = options.comparator ?? new NormalizedModelComparator();
    if (typeof comparator.name !== 'string' || !comparator.name.trim() || typeof comparator.compare !== 'function') throw new TypeError('comparator requires a nonempty name and compare method');
    const candidate = Node.make({ kind: 'model_call', parent: this.#cursorId, payload, attempt: options.attempt ?? 0 });
    if (!this.store.exists(candidate.id)) throw new MissingNodeError(`missing recorded model call: ${candidate.id}`);
    const checked = verify(this.store, candidate.id);
    if (!checked.ok) throw new IntegrityError(checked.findings.map(item => item.message).join('; '));
    const recorded = this.store.get(candidate.id);
    if (!recorded.result || typeof recorded.result !== 'object' || Array.isArray(recorded.result) || !recorded.resultDigest || recorded.meta.state === 'pending' || recorded.meta.state === 'failed' || recorded.meta.dry_run === true) throw new IntegrityError('recorded model result is not a replayable object');
    const livePayload = deepFreeze(snapshot(options.livePayload ?? payload, true)), liveContract = options.contract.toDict();
    if (options.livePayload) { const bound = extractReplayContract(livePayload); if (bound && canonicalText(bound) !== canonicalText(liveContract)) throw new TypeError('live payload replay contract does not match live contract'); }
    const observationId = options.observationId ?? randomUUID().replaceAll('-', '');
    const marked = makeRevalidationPayload(livePayload, { observationId, recordedNodeId: recorded.id, recordedResultDigest: recorded.resultDigest, contract: options.contract, comparatorName: comparator.name });
    const liveCandidate = Node.make({ kind: 'model_call', parent: this.#cursorId, payload: marked });
    if (this.store.exists(liveCandidate.id)) throw new DuplicateRecordingError(`revalidation observation already exists: ${observationId}`);
    return { recorded, livePayload, marked, observationId, comparator, liveContract, recordedContract: extractReplayContract(recorded.payload) };
  }
  #finishRevalidation(prepared: PreparedRevalidation, live: Node): RevalidationReport {
    const { recorded, comparator, observationId, liveContract, recordedContract } = prepared;
    const common: IdentityPayload = { format: REVALIDATION_FORMAT, observation_id: observationId, recorded_node_id: recorded.id, live_node_id: live.id, recorded_result_digest: recorded.resultDigest!, live_result_digest: live.resultDigest!, comparator: comparator.name };
    busyStores.add(this.store);
    try {
      const comparison = comparator.compare(snapshot(recorded.result as JsonObject), snapshot(live.result as JsonObject));
      if (!(comparison instanceof RevalidationComparison)) throw new TypeError('comparator must return RevalidationComparison');
      const exactMatch = recorded.resultText === live.resultText;
      const evidence = Node.make({ kind: 'note', parent: live.id, payload: { ...common, event: 'model_revalidation', comparison: comparison.toDict(), exact_match: exactMatch, live_contract: liveContract, ...(recordedContract ? { recorded_contract: recordedContract } : {}) }, meta: { created_at: new Date().toISOString() } });
      this.store.put(evidence); this.#runtime.notify(this.store.get(evidence.id));
      return deepFreeze({ observationId, recordedNodeId: recorded.id, liveNodeId: live.id, evidenceNodeId: evidence.id, comparator: comparator.name, matched: comparison.matched, exactMatch, recordedResultDigest: recorded.resultDigest!, liveResultDigest: live.resultDigest!, differencePaths: [...comparison.differencePaths], differencesTruncated: comparison.truncated, recordedContract, liveContract, charges: snapshot(live.meta.charges as JsonObject) as Charges });
    } catch (error) {
      try { const failure = Node.make({ kind: 'note', parent: live.id, payload: { ...common, event: 'model_revalidation_comparison_failed', error_type: error instanceof Error ? error.name : typeof error }, meta: { created_at: new Date().toISOString() } }); this.store.put(failure); this.#runtime.notify(this.store.get(failure.id)); }
      catch (cleanup) { this.#attachCleanup(error, cleanup); }
      throw error;
    } finally { this.#cursorId = recorded.id; busyStores.delete(this.store); }
  }
}

export class ReservationLeaseLost extends Error {
  override name = 'ReservationLeaseLost';
  constructor(message: string, readonly reservationId: string, readonly nodeId: string, readonly detail?: string) { super(message); }
}
