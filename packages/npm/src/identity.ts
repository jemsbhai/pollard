import { createHash } from 'node:crypto';
import { types } from 'node:util';

export type IdentityValue = null | boolean | number | string | IdentityValue[] | { [key: string]: IdentityValue };
export type IdentityPayload = { [key: string]: IdentityValue };
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export const DOMAIN = 'pollard/v1\n';
export const RESULT_DOMAIN = 'pollard/v1:result\n';
export const REDACTION_DOMAIN = 'pollard/v1:redact\n';

export function codePointCompare(a: string, b: string): number {
  const aa = Array.from(a, c => c.codePointAt(0)!);
  const bb = Array.from(b, c => c.codePointAt(0)!);
  for (let i = 0; i < Math.min(aa.length, bb.length); i++) {
    if (aa[i] !== bb[i]) return aa[i] - bb[i];
  }
  return aa.length - bb.length;
}

function validString(value: string, path: string): void {
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new TypeError(`lone surrogate at ${path}`);
    } else if (c >= 0xdc00 && c <= 0xdfff) throw new TypeError(`lone surrogate at ${path}`);
  }
}

function encode(value: unknown, floats: boolean, seen: Set<object>, path: string): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string') { validString(value, path); return JSON.stringify(value); }
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (!floats && !Number.isSafeInteger(value)) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
      throw new TypeError(`expected ${floats ? 'finite number or safe integer' : 'safe integer'} at ${path}`);
    }
    return JSON.stringify(value);
  }
  if (typeof value !== 'object') throw new TypeError(`unsupported JSON value at ${path}`);
  if (types.isProxy(value)) throw new TypeError(`proxy object at ${path}`);
  if (seen.has(value)) throw new TypeError(`cyclic JSON value at ${path}`);
  seen.add(value);
  try {
    if (Object.getOwnPropertySymbols(value).length) throw new TypeError(`symbol key at ${path}`);
    if (Array.isArray(value)) {
      const names = Object.getOwnPropertyNames(value);
      if (names.length !== value.length + 1 || !names.includes('length')) throw new TypeError(`sparse or extended array at ${path}`);
      const encoded: string[] = [];
      for (let i = 0; i < value.length; i++) {
        const descriptor = Object.getOwnPropertyDescriptor(value, String(i));
        if (!descriptor || !descriptor.enumerable || !('value' in descriptor)) throw new TypeError(`invalid array at ${path}`);
        encoded.push(encode(descriptor.value, floats, seen, `${path}[${i}]`));
      }
      return `[${encoded.join(',')}]`;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) throw new TypeError(`nonplain object at ${path}`);
    const keys = Object.getOwnPropertyNames(value).sort(codePointCompare);
    return `{${keys.map(key => {
      validString(key, path);
      const descriptor = Object.getOwnPropertyDescriptor(value, key)!;
      if (!descriptor.enumerable || !('value' in descriptor)) throw new TypeError(`accessor or hidden field at ${path}.${key}`);
      return `${JSON.stringify(key)}:${encode(descriptor.value, floats, seen, `${path}.${key}`)}`;
    }).join(',')}}`;
  } finally { seen.delete(value); }
}

/** Python-compatible canonical identity JSON over the portable safe-integer subset. */
export function canonicalText(value: IdentityValue): string { return encode(value, false, new Set(), '$'); }
export function canonicalBytes(value: IdentityValue): Uint8Array { return Buffer.from(canonicalText(value), 'utf8'); }
export function resultToText(value: JsonValue): string { return encode(value, true, new Set(), '$'); }
export function sha256(text: string): string { return createHash('sha256').update(text, 'utf8').digest('hex'); }
export function digestPayload(payload: IdentityValue): string { return sha256(canonicalText(payload)); }
export function nonnegativeInteger(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new TypeError(`${label} must be a non-negative safe integer`);
  return value;
}
export function checkedAdd(a: number, b: number, label: string): number { return nonnegativeInteger(a + b, label); }
export function nodeId(kind: string, parent: string | null, attempt: number, payload: IdentityPayload): string {
  nonnegativeInteger(attempt, 'attempt');
  return sha256(DOMAIN + canonicalText({ a: attempt, k: kind, p: parent ?? '', pl: payload }));
}
export function resultDigestFromText(text: string): string {
  if (typeof text !== 'string') throw new TypeError('result text must be a string');
  validString(text, 'result_text');
  return sha256(RESULT_DOMAIN + text);
}
export function resultTextAndDigest(value: JsonValue): [string, string] {
  const text = resultToText(value);
  return [text, resultDigestFromText(text)];
}
export function snapshot<T extends JsonValue>(value: T, identity = false): T {
  return JSON.parse(identity ? canonicalText(value) : resultToText(value)) as T;
}
export function deepFreeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
export function objectPayload(value: unknown, label = 'payload'): asserts value is IdentityPayload {
  if (value === null || Array.isArray(value) || typeof value !== 'object') throw new TypeError(`${label} must be a JSON object`);
  canonicalText(value as IdentityPayload);
}
export function redact(value: IdentityValue, hint: string | null = null): IdentityPayload {
  canonicalText(hint);
  return { __pollard_redacted: sha256(REDACTION_DOMAIN + canonicalText(value)), hint };
}
export function isRedacted(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const v = value as Record<string, unknown>;
  return Object.keys(v).length === 2 && Object.hasOwn(v, '__pollard_redacted') && Object.hasOwn(v, 'hint') &&
    typeof v.__pollard_redacted === 'string' && /^[a-f0-9]{64}$/.test(v.__pollard_redacted) && (v.hint === null || typeof v.hint === 'string');
}
