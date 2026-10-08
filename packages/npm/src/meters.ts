import { deepFreeze, IdentityPayload, JsonObject, nonnegativeInteger, objectPayload, sha256, snapshot } from './identity.js';
import type { NodeKind } from './tree.js';

export type ChargeAmount = number;
export type Charges = Record<string, number>;
export interface Estimator { estimateInputTokens(payload: IdentityPayload): number | null; }
export interface Measurement { start(): void; stop(): void; readings(): JsonObject; }
export interface Meter {
  readonly name: string;
  readonly precheckIsEstimate?: boolean;
  precheckEstimate(kind: NodeKind, payload: IdentityPayload): number | null;
  charge(kind: NodeKind, payload: IdentityPayload, result: JsonObject | null, meta: JsonObject): number;
  precheckFallbackReason?(kind: NodeKind, payload: IdentityPayload, result: JsonObject, meta: JsonObject): string | null;
  measure?(): Measurement;
}
export function chargeAmount(value: unknown, label = 'charge'): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > Number.MAX_SAFE_INTEGER) throw new TypeError(`${label} must be a finite non-negative number within the safe range`);
  return value;
}
/** Decimal addition avoids rounding 0.1 + 0.2 over a 0.3 ceiling. */
export function addCharges(left: number, right: number): number {
  const parts = (value: number): [bigint, number] => {
    const [coefficient, exponent = '0'] = String(value).split('e');
    const fraction = coefficient.split('.')[1]?.length ?? 0;
    return [BigInt(coefficient.replace('.', '')), fraction - Number(exponent)];
  };
  const [a, sa] = parts(left), [b, sb] = parts(right), scale = Math.max(sa, sb, 0);
  return chargeAmount(Number(`${a * 10n ** BigInt(scale - sa) + b * 10n ** BigInt(scale - sb)}e-${scale}`), 'charge total');
}
/** Price an integer token volume at a decimal rate per million. */
export function priceTokens(tokens: number, rate: number): number {
  nonnegativeInteger(tokens, 'priced tokens'); chargeAmount(rate, 'token price');
  const [coefficient, exponent = '0'] = String(rate).split('e');
  const scale = (coefficient.split('.')[1]?.length ?? 0) - Number(exponent) + 6;
  return chargeAmount(Number(`${BigInt(tokens) * BigInt(coefficient.replace('.', ''))}e${-scale}`), 'token cost');
}
export function compatibleUsage(result: JsonObject | null): { input_tokens: number; output_tokens: number } | null {
  const usage = result?.usage;
  if (!usage || typeof usage !== 'object' || Array.isArray(usage)) return null;
  try {
    const input_tokens = nonnegativeInteger(usage.input_tokens, 'input_tokens');
    const output_tokens = nonnegativeInteger(usage.output_tokens, 'output_tokens');
    nonnegativeInteger(input_tokens + output_tokens, 'total tokens');
    return { input_tokens, output_tokens };
  } catch { return null; }
}
const RESERVED = new Set(['accounting_fallbacks','avoided','charges','created_at','duration_s','reservation_id','reservation_lease','settlement','usage','state','accounting_unknown','outcome']);
export class MeterPrecheckRefusal extends Error {
  override name = 'MeterPrecheckRefusal';
  readonly reason: string;
  readonly detail: string;
  readonly requested?: string;
  readonly remaining?: string;
  readonly auditMeta?: JsonObject;
  constructor(reason: string, detail?: string, options: { auditMeta?: JsonObject; requested?: number | string; remaining?: number | string } = {}) {
    if (typeof reason !== 'string' || !reason || (detail !== undefined && (typeof detail !== 'string' || !detail))) throw new TypeError('refusal reason and detail must be nonempty strings');
    super(detail ?? reason); this.reason = reason; this.detail = detail ?? reason;
    for (const key of ['requested','remaining'] as const) if (options[key] !== undefined) {
      const value = options[key];
      if (!['number','string'].includes(typeof value) || String(value).trim() === '' || !Number.isFinite(Number(value))) throw new TypeError(`${key} must be finite and numeric`);
      this[key] = String(value);
    }
    if (options.auditMeta !== undefined) {
      if (!options.auditMeta || typeof options.auditMeta !== 'object' || Array.isArray(options.auditMeta)) throw new TypeError('auditMeta must be an object');
      for (const key of Object.keys(options.auditMeta)) if (RESERVED.has(key)) throw new TypeError(`auditMeta cannot override runtime metadata: ${key}`);
      this.auditMeta = deepFreeze(snapshot(options.auditMeta));
    }
    Object.freeze(this);
  }
}
export class StepMeter implements Meter {
  readonly name = 'steps';
  precheckEstimate(kind: NodeKind): number { return kind === 'model_call' || kind === 'tool_call' ? 1 : 0; }
  charge(kind: NodeKind): number { return this.precheckEstimate(kind); }
}
export class DepthMeter implements Meter {
  readonly name = 'depth';
  precheckEstimate(): null { return null; }
  charge(): number { return 0; }
}
export class WallClockMeter implements Meter {
  readonly name = 'seconds';
  precheckEstimate(): null { return null; }
  charge(_kind: NodeKind, _payload: IdentityPayload, _result: JsonObject | null, meta: JsonObject): number { return chargeAmount(meta.duration_s ?? 0, 'duration_s'); }
}
export class TokenMeter implements Meter {
  readonly name = 'tokens';
  readonly precheckIsEstimate: boolean;
  readonly estimator?: Estimator | ((payload: IdentityPayload) => number | null);
  readonly reservedOutputTokens: number;
  constructor(options: { estimator?: Estimator | ((payload: IdentityPayload) => number | null); reservedOutputTokens?: number } = {}) {
    this.estimator = options.estimator;
    if (this.estimator !== undefined && typeof this.estimator !== 'function' && typeof this.estimator.estimateInputTokens !== 'function') throw new TypeError('estimator must expose estimateInputTokens');
    this.reservedOutputTokens = nonnegativeInteger(options.reservedOutputTokens ?? 0, 'reservedOutputTokens');
    this.precheckIsEstimate = this.estimator !== undefined;
  }
  precheckEstimate(kind: NodeKind, payload: IdentityPayload): number | null {
    if (kind !== 'model_call' || !this.estimator) return null;
    const estimate = typeof this.estimator === 'function' ? this.estimator(payload) : this.estimator.estimateInputTokens(payload);
    return estimate === null ? null : nonnegativeInteger(nonnegativeInteger(estimate, 'token estimator result') + this.reservedOutputTokens, 'token estimate');
  }
  charge(kind: NodeKind, _payload: IdentityPayload, result: JsonObject | null): number {
    if (kind !== 'model_call' && kind !== 'tool_call') return 0;
    const usage = compatibleUsage(result); return usage ? usage.input_tokens + usage.output_tokens : 0;
  }
}
export interface ModelPrice { inputPer1m?: number; outputPer1m?: number; input_per_1m?: number; output_per_1m?: number; }
export class CostMeter implements Meter {
  readonly name = 'usd';
  readonly #prices: Record<string, { input: number; output: number }>;
  constructor(prices: Record<string, ModelPrice>) {
    snapshot(prices as unknown as JsonObject);
    this.#prices = Object.fromEntries(Object.entries(prices).map(([model, row]) => [model, { input: chargeAmount(row.inputPer1m ?? row.input_per_1m, 'input price'), output: chargeAmount(row.outputPer1m ?? row.output_per_1m, 'output price') }]));
  }
  precheckEstimate(): null { return null; }
  charge(kind: NodeKind, payload: IdentityPayload, result: JsonObject | null): number {
    const usage = compatibleUsage(result), price = typeof payload.model === 'string' && Object.hasOwn(this.#prices, payload.model) ? this.#prices[payload.model] : undefined;
    return kind !== 'model_call' || !usage || !price ? 0 : addCharges(priceTokens(usage.input_tokens, price.input), priceTokens(usage.output_tokens, price.output));
  }
}
export class WindowMeter implements Meter {
  readonly name: string;
  readonly limit: number;
  readonly windowSeconds: number;
  readonly meter: Meter;
  get precheckIsEstimate(): boolean { return this.meter.precheckIsEstimate === true; }
  constructor(name: string, limit: number, windowSeconds: number, options: { meter?: Meter } = {}) {
    if (typeof name !== 'string' || !name) throw new TypeError('window name must be nonempty');
    this.name = name; this.limit = chargeAmount(limit, 'window limit'); this.windowSeconds = chargeAmount(windowSeconds, 'windowSeconds');
    if (!this.limit || !this.windowSeconds) throw new TypeError('window limit and duration must be positive');
    this.meter = options.meter ?? (name === 'tokens' ? new TokenMeter() : new StepMeter());
  }
  precheckEstimate(kind: NodeKind, payload: IdentityPayload): number | null { return this.meter.precheckEstimate(kind, payload); }
  charge(kind: NodeKind, payload: IdentityPayload, result: JsonObject | null, meta: JsonObject): number { return this.meter.charge(kind, payload, result, meta); }
  ledgerKey(rootId: string): string {
    const seconds = this.windowSeconds < 0.0001 ? this.windowSeconds.toExponential().replace(/e([+-])(\d+)$/, (_all, sign: string, exponent: string) => `e${sign}${exponent.padStart(2, '0')}`) : Number.isInteger(this.windowSeconds) ? `${this.windowSeconds}.0` : String(this.windowSeconds);
    return sha256(JSON.stringify({ limit: String(this.limit).replace('e', 'E'), name: this.name, root_id: rootId, window_seconds: seconds }));
  }
}
