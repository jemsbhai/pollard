import { IdentityPayload, JsonObject, JsonValue, nonnegativeInteger, snapshot } from './identity.js';
import { addCharges, chargeAmount, DepthMeter, Estimator, Meter, MeterPrecheckRefusal, priceTokens, StepMeter, WallClockMeter } from './meters.js';
import type { NodeKind } from './tree.js';

export interface ExclusiveUsage extends JsonObject { input_tokens: number; cache_read_tokens: number; cache_write_tokens: number; output_tokens: number; reasoning_tokens: number; }
export type TokenmasterTarget = string | { model_id: string; [key: string]: unknown };
export interface TokenmasterRequestCheck extends JsonObject {
  allowed: boolean; model_id: string; context_output_tokens: number; violations: string[];
  input_exceeded: boolean; context_exceeded: boolean; output_exceeded: boolean;
  input_tokens: number; max_input_tokens: number; context_tokens: number; capacity: number;
  requested_output_tokens: number; max_output_tokens: number;
}
export interface TokenmasterEstimateQuote extends JsonObject { model_id: string; currency: string; input_tokens: number; reserved_output_tokens: number; input_rate: number; output_rate: number; }
export interface TokenmasterUsageQuote extends JsonObject { model_id: string; currency: string; pricing: { input: number; cache_read: number; cache_write: number; output: number }; }
export type TokenmasterDocument = JsonObject | { toDict(): JsonObject };
export interface TokenmasterGauge {
  readonly profile?: TokenmasterTarget;
  record(turn: ExclusiveUsage): TokenmasterDocument & { contextTotal?(): number };
  state(): TokenmasterDocument;
  advise(options: { task?: unknown; policy?: unknown }): TokenmasterDocument;
}
/** Explicit host integration. No Python process or implicit model-price registry is used. */
export interface TokenmasterClient {
  getProfile(target: string): TokenmasterTarget;
  createMeter(profile: TokenmasterTarget, options: { reservedOutput: number }): TokenmasterGauge;
  checkRequestLimits(target: TokenmasterTarget, options: { inputTokens: number; requestedOutputTokens: number | null; reservedOutputTokens: number; capacity: 'nominal' | 'effective' }): TokenmasterRequestCheck;
  quoteEstimate(target: TokenmasterTarget, options: { inputTokens: number; reservedOutputTokens: number; conservative: true }): TokenmasterEstimateQuote;
  quoteUsage(target: TokenmasterTarget, turn: ExclusiveUsage): TokenmasterUsageQuote;
  createTask?(options: { expectedRemainingTurns: number }): unknown;
}
export interface TokenmasterMeterOptions {
  client: TokenmasterClient; model?: string; meter?: TokenmasterGauge;
  estimator?: Estimator | ((payload: IdentityPayload) => number | null); reservedOutput?: number;
  expectedRemainingTurns?: number; task?: unknown; policy?: unknown;
  enforceProfileLimits?: boolean; profileCapacity?: 'nominal' | 'effective';
}
export interface TokenmasterCostMeterOptions {
  client: TokenmasterClient; model?: string; meter?: TokenmasterGauge;
  estimator: Estimator | ((payload: IdentityPayload) => number | null); reservedOutput?: number; name?: string;
}
function object(value: unknown): value is JsonObject { return !!value && typeof value === 'object' && !Array.isArray(value); }
function integer(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0; }
function pick(usage: JsonObject, ...keys: string[]): number { for (const key of keys) if (integer(usage[key])) return usage[key] as number; return 0; }
function details(usage: JsonObject, ...keys: string[]): JsonObject { for (const key of keys) if (object(usage[key])) return usage[key] as JsonObject; return {}; }
function total(turn: ExclusiveUsage): number { return nonnegativeInteger(turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens + turn.output_tokens + turn.reasoning_tokens, 'exclusive usage total'); }
/** Convert OpenAI inclusive details and Anthropic/Bedrock exclusive cache fields. */
export function exclusiveTurnUsage(result: JsonObject): ExclusiveUsage {
  if (!object(result.usage)) return { input_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 0, reasoning_tokens: 0 };
  const normalized = object(result.usage) ? result.usage : {};
  const raw = object(result.provider_usage) ? result.provider_usage : undefined;
  if (!raw || ['cache_read_tokens','cache_read_input_tokens','cacheReadInputTokens','cache_write_tokens','cache_creation_input_tokens','cache_write_input_tokens','cacheWriteInputTokens'].some(key => Object.hasOwn(raw, key))) {
    const source = raw ?? normalized;
    return { input_tokens: pick(source, 'input_tokens','inputTokens','prompt_tokens'), cache_read_tokens: pick(source, 'cache_read_tokens','cached_input_tokens','cache_read_input_tokens','cacheReadInputTokens'), cache_write_tokens: pick(source, 'cache_write_tokens','cache_creation_input_tokens','cache_write_input_tokens','cacheWriteInputTokens'), output_tokens: pick(source,'output_tokens','outputTokens','completion_tokens'), reasoning_tokens: pick(source,'reasoning_tokens') };
  }
  const input = ['input_tokens','prompt_tokens'].some(key => integer(raw[key])) ? pick(raw,'input_tokens','prompt_tokens') : pick(normalized,'input_tokens','prompt_tokens');
  const output = ['output_tokens','completion_tokens'].some(key => integer(raw[key])) ? pick(raw,'output_tokens','completion_tokens') : pick(normalized,'output_tokens','completion_tokens');
  const inputDetails = details(raw,'input_tokens_details','prompt_tokens_details'), outputDetails = details(raw,'output_tokens_details','completion_tokens_details');
  const cacheRead = Math.min(input, pick(inputDetails,'cached_tokens') || pick(raw,'cached_input_tokens'));
  const cacheWrite = Math.min(input - cacheRead, pick(inputDetails,'cache_write_tokens') || pick(raw,'cache_write_tokens','cache_creation_input_tokens'));
  const reasoning = Math.min(output, pick(outputDetails,'reasoning_tokens') || pick(raw,'reasoning_tokens'));
  return { input_tokens: input - cacheRead - cacheWrite, cache_read_tokens: cacheRead, cache_write_tokens: cacheWrite, output_tokens: output - reasoning, reasoning_tokens: reasoning };
}
function estimateInput(estimator: NonNullable<TokenmasterMeterOptions['estimator']>, payload: IdentityPayload): number | null {
  const estimate = typeof estimator === 'function' ? estimator(payload) : estimator.estimateInputTokens(payload);
  return estimate === null ? null : nonnegativeInteger(estimate, 'token estimator result');
}
function requestedOutput(payload: IdentityPayload): number | null {
  for (const key of ['max_output_tokens','max_completion_tokens','max_tokens']) if (Object.hasOwn(payload, key)) return nonnegativeInteger(payload[key], key);
  return null;
}
function modelId(target: TokenmasterTarget | undefined): string | undefined { return typeof target === 'string' ? target : target?.model_id; }
function targetFor(options: Pick<TokenmasterMeterOptions,'model'|'meter'>, payload: IdentityPayload, result?: JsonObject): TokenmasterTarget | undefined {
  if (options.meter?.profile) return options.meter.profile;
  if (options.model) return options.model;
  const usage = result && object(result.usage) ? result.usage : {};
  for (const value of [result?.model, usage.model_id, payload.model]) if (typeof value === 'string' && value) return value;
  return undefined;
}
function document(value: TokenmasterDocument): JsonObject { return snapshot(typeof (value as {toDict?: unknown}).toDict === 'function' ? (value as {toDict(): JsonObject}).toDict() : value as JsonObject); }
function audit(meta: JsonObject): JsonObject { if (!object(meta.tokenmaster)) meta.tokenmaster = {}; return meta.tokenmaster as JsonObject; }
function errorType(error: unknown): string { return error instanceof Error ? error.name : typeof error; }
function validateOptions(options: TokenmasterMeterOptions | TokenmasterCostMeterOptions): void {
  if (!options.client || typeof options.client !== 'object') throw new TypeError('a synchronous TokenmasterClient must be explicitly supplied');
  if (options.model !== undefined && (typeof options.model !== 'string' || !options.model)) throw new TypeError('model must be a nonempty string');
  if (options.model !== undefined && options.meter !== undefined) throw new TypeError('pass either model or meter, not both');
  nonnegativeInteger(options.reservedOutput ?? 0, 'reservedOutput');
  if (options.estimator !== undefined && typeof options.estimator !== 'function' && typeof options.estimator.estimateInputTokens !== 'function') throw new TypeError('estimator must expose estimateInputTokens');
}

export class TokenmasterMeter implements Meter {
  readonly name = 'tokens';
  readonly precheckIsEstimate: boolean;
  readonly #options: Readonly<TokenmasterMeterOptions>;
  readonly #meters = new Map<string, TokenmasterGauge>();
  #task: unknown;
  constructor(options: TokenmasterMeterOptions) {
    validateOptions(options);
    if (options.expectedRemainingTurns !== undefined && options.task !== undefined) throw new TypeError('pass either expectedRemainingTurns or task');
    if (options.expectedRemainingTurns !== undefined) nonnegativeInteger(options.expectedRemainingTurns, 'expectedRemainingTurns');
    if (options.enforceProfileLimits !== undefined && typeof options.enforceProfileLimits !== 'boolean') throw new TypeError('enforceProfileLimits must be boolean');
    if (!['nominal','effective'].includes(options.profileCapacity ?? 'nominal')) throw new TypeError('profileCapacity must be nominal or effective');
    if (options.enforceProfileLimits && !options.estimator) throw new TypeError('profile-limit enforcement requires an estimator');
    this.#options = Object.freeze({ ...options }); this.#task = options.task; this.precheckIsEstimate = options.estimator !== undefined;
  }
  precheckEstimate(kind: NodeKind, payload: IdentityPayload): number | null {
    const options = this.#options;
    if (kind !== 'model_call' || !options.estimator) return null;
    const estimate = estimateInput(options.estimator, payload);
    if (!options.enforceProfileLimits) return estimate === null ? null : nonnegativeInteger(estimate + (options.reservedOutput ?? 0), 'token estimate');
    const target = targetFor(options, payload), model = modelId(target);
    const unavailable = (reason: string, detail: string, error?: unknown): never => { throw new MeterPrecheckRefusal('tokenmaster_profile_unavailable', detail, { auditMeta: { meter: this.name, tokenmaster: { ...(model ? {model_id: model} : {}), limits: { status: 'unavailable', reason, ...(error === undefined ? {} : { error: errorType(error) }) } } } }); };
    if (estimate === null) unavailable('missing_input_estimate', 'tokenmaster profile enforcement needs an input-token estimate');
    if (!target) unavailable('missing_model', 'tokenmaster profile enforcement needs a model id');
    const requested = requestedOutput(payload); let check: TokenmasterRequestCheck;
    try { check = options.client.checkRequestLimits(target!, { inputTokens: estimate!, requestedOutputTokens: requested, reservedOutputTokens: options.reservedOutput ?? 0, capacity: options.profileCapacity ?? 'nominal' }); snapshot(check); }
    catch (error) { unavailable('profile_lookup_failed', 'tokenmaster could not resolve request limits', error); }
    if (typeof check!.allowed !== 'boolean') unavailable('profile_lookup_failed', 'tokenmaster returned an invalid request limit check');
    if (!check!.allowed) {
      const requested = check!.input_exceeded ? check!.input_tokens : check!.context_exceeded ? check!.context_tokens : check!.requested_output_tokens;
      const remaining = check!.input_exceeded ? check!.max_input_tokens : check!.context_exceeded ? check!.capacity : check!.max_output_tokens;
      throw new MeterPrecheckRefusal('tokenmaster_profile_limit', 'tokenmaster model profile refused the request', { requested, remaining, auditMeta: { meter: this.name, tokenmaster: { model_id: check!.model_id, limits: snapshot(check!) } } });
    }
    return nonnegativeInteger(estimate! + nonnegativeInteger(check!.context_output_tokens, 'context_output_tokens'), 'token estimate');
  }
  charge(kind: NodeKind, payload: IdentityPayload, result: JsonObject | null, meta: JsonObject): number {
    if (kind !== 'model_call' || !result || !object(result.usage)) return 0;
    const options = this.#options, turn = exclusiveTurnUsage(result), charge = total(turn), target = targetFor(options, payload, result), metadata = audit(meta);
    if (options.enforceProfileLimits) {
      try {
        metadata.limits = target ? { ...options.client.checkRequestLimits(target, { inputTokens: turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens, requestedOutputTokens: turn.output_tokens + turn.reasoning_tokens, reservedOutputTokens: options.reservedOutput ?? 0, capacity: options.profileCapacity ?? 'nominal' }), phase: 'settlement' } : { status: 'unavailable', reason: 'missing_model' };
      } catch (error) { metadata.limits = { status: 'unavailable', reason: 'profile_lookup_failed', error: errorType(error) }; }
    }
    try {
      let meter = options.meter;
      if (!meter && target) {
        const profile = typeof target === 'string' ? options.client.getProfile(target) : target, model = modelId(profile);
        meter = model ? this.#meters.get(model) : undefined;
        if (!meter) { meter = options.client.createMeter(profile, { reservedOutput: options.reservedOutput ?? 0 }); if (model) this.#meters.set(model, meter); }
      }
      if (!meter) return charge;
      const profileModel = modelId(meter.profile); if (profileModel) turn.model_id = profileModel;
      const recorded = meter.record(turn), state = meter.state();
      if (this.#task === undefined && options.expectedRemainingTurns !== undefined) this.#task = options.client.createTask?.({ expectedRemainingTurns: options.expectedRemainingTurns }) ?? { expected_remaining_turns: options.expectedRemainingTurns };
      const advice = meter.advise({ task: this.#task, policy: options.policy });
      Object.assign(metadata, { turn: document(recorded), state: document(state), advice: document(advice) });
      if (this.#task && typeof this.#task === 'object') try { metadata.task = document(this.#task as TokenmasterDocument); } catch {}
      return typeof recorded.contextTotal === 'function' ? nonnegativeInteger(recorded.contextTotal(), 'context total') : charge;
    } catch (error) { metadata.meter = { status: 'error', error: errorType(error) }; return charge; }
  }
}

export class TokenmasterCostMeter implements Meter {
  readonly name: string;
  readonly precheckIsEstimate = true;
  readonly #options: Readonly<TokenmasterCostMeterOptions>;
  constructor(options: TokenmasterCostMeterOptions) {
    validateOptions(options); if (!options.estimator) throw new TypeError('cost governance requires an estimator');
    this.name = options.name ?? 'usd'; if (typeof this.name !== 'string' || !this.name) throw new TypeError('cost meter name must be nonempty'); this.#options = Object.freeze({ ...options });
  }
  precheckEstimate(kind: NodeKind, payload: IdentityPayload): number | null {
    if (kind !== 'model_call') return null;
    const options = this.#options, estimate = estimateInput(options.estimator, payload), target = targetFor(options, payload), model = modelId(target);
    const refuse = (reason: string, detail: string, error?: unknown): never => { throw new MeterPrecheckRefusal('tokenmaster_pricing_unavailable', detail, { auditMeta: { meter: this.name, tokenmaster: { ...(model ? { model_id: model } : {}), pricing: { status: 'unavailable', reason, ...(error === undefined ? {} : { error: errorType(error) }) } } } }); };
    if (estimate === null) refuse('missing_input_estimate', 'tokenmaster USD governance needs an input-token estimate');
    if (!target) refuse('missing_model', 'tokenmaster USD governance needs a model id');
    const output = Math.max(options.reservedOutput ?? 0, requestedOutput(payload) ?? 0);
    let quote: TokenmasterEstimateQuote;
    try { quote = options.client.quoteEstimate(target!, { inputTokens: estimate!, reservedOutputTokens: output, conservative: true }); snapshot(quote); }
    catch (error) { refuse('pricing_lookup_failed', 'tokenmaster could not conservatively price the request', error); }
    if (typeof quote!.currency !== 'string' || !quote!.currency || typeof quote!.model_id !== 'string') refuse('pricing_lookup_failed', 'tokenmaster returned incomplete pricing');
    if (quote!.currency !== 'USD') throw new MeterPrecheckRefusal('tokenmaster_currency', 'tokenmaster USD governance requires USD pricing', { auditMeta: { meter: this.name, tokenmaster: { model_id: quote!.model_id, pricing: { status: 'unsupported_currency', currency: quote!.currency } } } });
    try {
      if (nonnegativeInteger(quote!.input_tokens, 'quoted input tokens') < estimate! || nonnegativeInteger(quote!.reserved_output_tokens, 'quoted output tokens') < output) throw new TypeError('quote undercounts the request');
      return addCharges(priceTokens(quote!.input_tokens, chargeAmount(quote!.input_rate, 'input rate')), priceTokens(quote!.reserved_output_tokens, chargeAmount(quote!.output_rate, 'output rate')));
    } catch (error) { return refuse('pricing_lookup_failed', 'tokenmaster returned incomplete pricing', error); }
  }
  charge(kind: NodeKind, payload: IdentityPayload, result: JsonObject | null, meta: JsonObject): number {
    if (kind !== 'model_call' || !result || !object(result.usage)) return 0;
    const target = targetFor(this.#options, payload, result), metadata = audit(meta);
    if (!target) { metadata.cost = { status: 'unavailable', reason: 'missing_model' }; return 0; }
    try {
      const turn = exclusiveTurnUsage(result), quote = this.#options.client.quoteUsage(target, turn);
      if (quote.currency !== 'USD') throw new TypeError('unsupported pricing currency');
      const pricing = quote.pricing;
      let amount = 0;
      for (const [category, rate] of [[turn.input_tokens, pricing.input], [turn.cache_read_tokens, pricing.cache_read], [turn.cache_write_tokens, pricing.cache_write], [turn.output_tokens, pricing.output], [turn.reasoning_tokens, pricing.output]]) amount = addCharges(amount, priceTokens(category, chargeAmount(rate, 'usage price')));
      metadata.cost = { ...snapshot(quote), status: 'quoted', total_cost_decimal: String(amount) }; return amount;
    } catch (error) { metadata.cost = { status: 'unavailable', reason: 'pricing_failed', error: errorType(error) }; return 0; }
  }
  precheckFallbackReason(kind: NodeKind, _payload: IdentityPayload, _result: JsonObject, meta: JsonObject): string | null {
    const cost = object(meta.tokenmaster) && object(meta.tokenmaster.cost) ? meta.tokenmaster.cost : undefined;
    return kind === 'model_call' && cost?.status === 'unavailable' ? 'exact_pricing_unavailable' : null;
  }
}
export function tokenmasterGovernanceMeters(options: TokenmasterMeterOptions & { estimator: NonNullable<TokenmasterMeterOptions['estimator']>; costName?: string }): Meter[] {
  return [new StepMeter(), new DepthMeter(), new WallClockMeter(), new TokenmasterMeter(options), new TokenmasterCostMeter({ client: options.client, model: options.model, meter: options.meter, estimator: options.estimator, reservedOutput: options.reservedOutput, name: options.costName })];
}
