import { checkedAdd, deepFreeze, digestPayload, IdentityPayload, JsonObject, nonnegativeInteger, objectPayload, snapshot } from './identity.js';
import { ActionSpec, Policy, Registry } from './registry.js';
import { BudgetExceeded, ConcurrentCallError, ConfirmationRequired, DuplicateRecordingError, IntegrityError, MemoryStore, MissingNodeError, Node, NodeKind, PolicyViolation, RecordingStore, Store, UsageError, validateNode, verify } from './tree.js';

export type ReplayMode = 'record' | 'hybrid' | 'replay';
export interface Budget { steps?: number; tokens?: number; depth?: number; }
export interface CallOptions { attempt?: number; tokenEstimate?: number; }
export interface ToolCallOptions extends CallOptions { version?: string; }
export type StepFn = (payload: IdentityPayload) => JsonObject;
export type AsyncStepFn = (payload: IdentityPayload) => JsonObject | Promise<JsonObject>;
export interface RuntimeOptions {
  store?: Store;
  mode?: ReplayMode;
  registry?: Registry;
  policies?: readonly Policy[];
  dryRun?: boolean;
  estimateTokens?: (payload: IdentityPayload) => number;
  reservedOutputTokens?: number;
}
interface Scope { budget: Budget; anchorId: string; }
interface Counters { steps: number; tokens: number; }
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
  objectPayload(value, 'budget');
  for (const key of Object.keys(value)) if (!['steps', 'tokens', 'depth'].includes(key)) throw new TypeError(`unsupported budget: ${key}`);
  for (const [key, amount] of Object.entries(value)) nonnegativeInteger(amount, `${key} budget`);
  return deepFreeze(snapshot(value as IdentityPayload, true)) as Budget;
}
function optionsCopy(value: ToolCallOptions): ToolCallOptions {
  objectPayload(value, 'call options');
  for (const key of Object.keys(value)) if (!['attempt', 'tokenEstimate', 'version'].includes(key)) throw new TypeError(`unsupported call option: ${key}`);
  if (value.attempt !== undefined) nonnegativeInteger(value.attempt, 'attempt');
  if (value.tokenEstimate !== undefined) nonnegativeInteger(value.tokenEstimate, 'tokenEstimate');
  if (value.version !== undefined && (typeof value.version !== 'string' || !value.version)) throw new TypeError('version must be a nonempty string');
  return deepFreeze(snapshot(value as IdentityPayload, true)) as ToolCallOptions;
}
function usageTokens(result: JsonObject): number | null {
  if (!Object.hasOwn(result, 'usage')) return null;
  const usage = result.usage;
  if (!usage || Array.isArray(usage) || typeof usage !== 'object') throw new UsageError('usage must be an object');
  try {
    return checkedAdd(nonnegativeInteger(usage.input_tokens, 'usage.input_tokens'), nonnegativeInteger(usage.output_tokens, 'usage.output_tokens'), 'usage total');
  } catch { throw new UsageError('usage token counts must be non-negative safe integers'); }
}
function counters(store: Store, anchorId: string): Counters {
  const total = { steps: 0, tokens: 0 };
  for (const node of store.walk(anchorId)) {
    validateNode(node);
    const charges = node.meta.charges;
    if (charges === undefined) continue;
    if (!charges || typeof charges !== 'object' || Array.isArray(charges)) throw new IntegrityError('invalid stored charges');
    for (const meter of ['steps', 'tokens'] as const) if (Object.hasOwn(charges, meter)) total[meter] = checkedAdd(total[meter], nonnegativeInteger(charges[meter], `stored ${meter} charge`), `${meter} total`);
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
}

export class Run {
  readonly #runtime: Runtime;
  readonly #rootId: string;
  readonly #label: string;
  #cursorId: string;
  readonly #scopes: Scope[];
  readonly #pending = new Map<string, PendingTool>();
  #avoided: Counters = { steps: 0, tokens: 0 };
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
  toolCall(name: string, args: IdentityPayload, fn?: StepFn, options: ToolCallOptions = {}): Node {
    this.#idle();
    if (!this.#runtime.registry) return this.#callSync('tool_call', { tool: name, args }, fn as StepFn, options);
    const prepared = this.#prepareTool(name, args, options);
    if ('recorded' in prepared) return prepared.recorded;
    if (this.#runtime.dryRun && prepared.spec.sideEffects) return this.#dryRun(prepared.payload, options);
    return this.#callSync('tool_call', prepared.payload, () => prepared.spec.handler!(prepared.args) as JsonObject, options, prepared.spec.handler);
  }
  async toolCallAsync(name: string, args: IdentityPayload, fn?: AsyncStepFn, options: ToolCallOptions = {}): Promise<Node> {
    this.#idle();
    if (!this.#runtime.registry) return this.#callAsync('tool_call', { tool: name, args }, fn as AsyncStepFn, options);
    const prepared = this.#prepareTool(name, args, options);
    if ('recorded' in prepared) return prepared.recorded;
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
    this.#precheck('note', candidate.payload, 0);
    this.store.put(candidate); this.#cursorId = candidate.id;
    return this.store.get(candidate.id);
  }
  /** A branch keeps its own cursor and shares ancestor spending. */
  branch(options: { attempt?: number; budget?: Budget } = {}): Run {
    this.#idle();
    const budget = options.budget === undefined ? undefined : budgetCopy(options.budget);
    const candidate = Node.make({ kind: 'note', parent: this.#cursorId, payload: { branch: true }, attempt: options.attempt ?? 0 });
    if (this.#runtime.mode === 'replay') this.#structuralReplay(candidate, false);
    else { this.#precheck('note', candidate.payload, 0); this.store.put(candidate); }
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

  #idle(): void { if (busyStores.has(this.store)) throw new ConcurrentCallError('store already has an active dispatch'); }
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
    if (charges && typeof charges === 'object' && !Array.isArray(charges)) for (const meter of ['steps', 'tokens'] as const) if (Object.hasOwn(charges, meter)) this.#avoided[meter] = checkedAdd(this.#avoided[meter], nonnegativeInteger(charges[meter], `stored ${meter} charge`), 'avoided charge');
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
  #tokenEstimate(kind: NodeKind, payload: IdentityPayload, options: CallOptions): number {
    if (options.tokenEstimate === undefined && !this.#runtime.estimateTokens && this.#scopes.some(scope => scope.budget.tokens !== undefined)) throw new TypeError('a token budget requires tokenEstimate or an estimateTokens callback');
    const estimate = options.tokenEstimate !== undefined ? nonnegativeInteger(options.tokenEstimate, 'tokenEstimate') :
      this.#runtime.estimateTokens ? nonnegativeInteger(this.#runtime.estimateTokens(deepFreeze(snapshot(payload, true))), 'token estimator result') : 0;
    return checkedAdd(estimate, kind === 'model_call' ? this.#runtime.reservedOutputTokens : 0, 'token estimate');
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
  #precheck(kind: NodeKind, payload: IdentityPayload, tokenEstimate: number): void {
    const call = kind === 'tool_call' || kind === 'model_call';
    const estimates = { steps: call ? 1 : 0, tokens: call ? tokenEstimate : 0, depth: this.#depth() };
    for (const scope of this.#scopes) {
      const spent = counters(this.store, scope.anchorId);
      for (const meter of ['steps', 'tokens', 'depth'] as const) {
        const limit = scope.budget[meter];
        if (limit === undefined) continue;
        const remaining = limit - (meter === 'depth' ? 0 : spent[meter]);
        if (meter === 'tokens' && call && [...this.store.walk(scope.anchorId)].some(node => node.meta.accounting_unknown === true)) this.#refuseBudget(kind, payload, meter, 'unknown', String(remaining));
        if (estimates[meter] > remaining) this.#refuseBudget(kind, payload, meter, String(estimates[meter]), String(remaining));
      }
    }
  }
  #refuseBudget(kind: NodeKind, payload: IdentityPayload, meter: string, requested: string, remaining: string): never {
    const refusal = Node.make({ kind: 'refusal', parent: this.#cursorId, payload: { reason: 'budget', meter, requested, remaining, blocked_kind: kind, blocked_payload_digest: digestPayload(payload) } });
    this.store.put(refusal); this.#cursorId = refusal.id;
    throw new BudgetExceeded(`budget exceeded for ${meter}`, refusal.id);
  }
  #refusePolicy(detail: string, payload: IdentityPayload): never {
    const refusalPayload: IdentityPayload = { reason: 'policy', detail, blocked_kind: 'tool_call', blocked_payload_digest: digestPayload(payload) };
    if (this.#runtime.registry) refusalPayload.registry_digest = this.#runtime.registry.registryDigest;
    const refusal = Node.make({ kind: 'refusal', parent: this.#cursorId, payload: refusalPayload });
    if (this.#runtime.mode === 'replay') this.#structuralReplay(refusal);
    else { this.store.put(refusal); this.#cursorId = refusal.id; }
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
      const estimate = this.#tokenEstimate('tool_call', payload, options);
      this.#precheck('tool_call', payload, estimate);
      const node = Node.make({ kind: 'tool_call', parent: this.#cursorId, payload, attempt: options.attempt ?? 0, meta: { dry_run: true, charges: { steps: 1, tokens: 0 } } });
      if (this.store.exists(node.id)) throw new DuplicateRecordingError(`recording appeared before dispatch: ${node.id}`);
      this.store.put(node); this.#cursorId = node.id;
      return this.store.get(node.id);
    } finally { busyStores.delete(this.store); }
  }
  #begin(kind: NodeKind, payload: IdentityPayload, options: CallOptions): { pending: Node; estimate: number; recording: RecordingStore } {
    const store = this.store as RecordingStore;
    if (typeof store.finalize !== 'function') throw new TypeError('live execution requires the optional RecordingStore.finalize capability');
    const estimate = this.#tokenEstimate(kind, payload, options);
    this.#precheck(kind, payload, estimate);
    const pending = Node.make({ kind, parent: this.#cursorId, payload, attempt: options.attempt ?? 0,
      meta: { state: 'pending', accounting_unknown: true, charges: { steps: 1, tokens: estimate } } });
    // Callbacks may have changed trusted store state after the first lookup.
    // Never rely on an idempotent put as proof this dispatch owns the identity.
    if (store.exists(pending.id)) throw new DuplicateRecordingError(`recording appeared before dispatch: ${pending.id}`);
    store.put(pending);
    return { pending, estimate, recording: store };
  }
  #fail(pending: Node): void {
    this.store.updateMeta(pending.id, { state: 'failed', accounting_unknown: true, outcome: 'unknown' });
  }
  #finish(pending: Node, result: JsonObject, estimate: number, recording: RecordingStore): Node {
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new TypeError('handler result must be a JSON object; streaming is unsupported');
    const copied = snapshot(result);
    let tokens: number | null = null;
    let usageError: UsageError | undefined;
    try { tokens = usageTokens(copied); } catch (error) { usageError = error as UsageError; }
    const requiresUsage = this.#scopes.some(scope => scope.budget.tokens !== undefined);
    if (tokens === null && requiresUsage && !usageError) usageError = new UsageError('a token budget requires input_tokens and output_tokens usage');
    const meta: JsonObject = { state: 'completed', accounting_unknown: tokens === null, charges: { steps: 1, tokens: tokens ?? estimate } };
    const complete = Node.make({ kind: pending.kind, parent: pending.parent, payload: pending.payload, attempt: pending.attempt, result: copied, meta });
    recording.finalize(complete); this.#cursorId = complete.id;
    if (usageError) throw usageError;
    return this.store.get(complete.id);
  }
  #callSync(kind: NodeKind, payload: IdentityPayload, fn: StepFn, options: CallOptions, originalFn?: unknown): Node {
    this.#idle();
    options = optionsCopy(options);
    objectPayload(payload);
    const clean = deepFreeze(snapshot(payload, true));
    const recorded = this.#recorded(kind, clean, options);
    if (recorded) return recorded;
    checkFunction(originalFn ?? fn, false);
    busyStores.add(this.store);
    let pending: Node | undefined;
    let held = false;
    try {
      const begun = this.#begin(kind, clean, options); pending = begun.pending;
      const result = fn(clean);
      if (isPromise(result)) {
        held = true;
        Promise.resolve(result).catch(() => undefined).finally(() => busyStores.delete(this.store));
        throw new TypeError('handler returned a promise; use an async call method');
      }
      return this.#finish(pending, result, begun.estimate, begun.recording);
    } catch (error) {
      if (pending && this.store.get(pending.id).meta.state === 'pending') this.#fail(pending);
      throw error;
    } finally { if (!held) busyStores.delete(this.store); }
  }
  async #callAsync(kind: NodeKind, payload: IdentityPayload, fn: AsyncStepFn, options: CallOptions): Promise<Node> {
    this.#idle();
    options = optionsCopy(options);
    objectPayload(payload);
    const clean = deepFreeze(snapshot(payload, true));
    const recorded = this.#recorded(kind, clean, options);
    if (recorded) return recorded;
    checkFunction(fn, true);
    busyStores.add(this.store);
    let pending: Node | undefined;
    try {
      const begun = this.#begin(kind, clean, options); pending = begun.pending;
      const result = await fn(clean);
      return this.#finish(pending, result, begun.estimate, begun.recording);
    } catch (error) {
      if (pending && this.store.get(pending.id).meta.state === 'pending') this.#fail(pending);
      throw error;
    } finally { busyStores.delete(this.store); }
  }
}
