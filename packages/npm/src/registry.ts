import { canonicalText, codePointCompare, deepFreeze, digestPayload, IdentityPayload, IdentityValue, JsonObject, objectPayload, redact, snapshot } from './identity.js';
import { UnsupportedSchema } from './tree.js';

const KEYS = new Set(['type', 'properties', 'required', 'enum', 'anyOf', 'items', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength', 'minItems', 'maxItems', 'additionalProperties', 'title', 'description', 'default', 'sensitive']);
const TYPES = new Set(['object', 'string', 'integer', 'boolean', 'array', 'null']);
type Schema = IdentityPayload;
export type ActionHandler = (args: IdentityPayload) => JsonObject | Promise<JsonObject>;
export interface ActionSpecInput {
  name: string;
  version: string;
  description: string;
  schema: Schema;
  sideEffects: boolean;
  handler?: ActionHandler;
}

function sensitiveString(schema: Schema): boolean {
  if (schema.type === 'string') return true;
  if (!Array.isArray(schema.anyOf) || schema.anyOf.length === 0) return false;
  const types = schema.anyOf.map(branch => branch && typeof branch === 'object' && !Array.isArray(branch) ? branch.type : null);
  return types.includes('string') && types.every(type => type === 'string' || type === 'null');
}

function checkSchema(schema: unknown, path: string): asserts schema is Schema {
  try { objectPayload(schema, path); } catch (error) { throw new UnsupportedSchema(error instanceof Error ? error.message : `${path}: invalid schema`); }
  for (const key of Object.keys(schema)) if (!KEYS.has(key)) throw new UnsupportedSchema(`${path}: unsupported keyword ${key}`);
  if (Object.hasOwn(schema, 'type') && (typeof schema.type !== 'string' || !TYPES.has(schema.type))) throw new UnsupportedSchema(`${path}: unsupported type`);
  if (Object.hasOwn(schema, 'properties')) {
    const properties = schema.properties;
    if (!properties || Array.isArray(properties) || typeof properties !== 'object') throw new UnsupportedSchema(`${path}.properties: must be an object`);
    for (const [key, child] of Object.entries(properties)) checkSchema(child, `${path}.properties.${key}`);
  }
  if (Object.hasOwn(schema, 'required') && (!Array.isArray(schema.required) || !schema.required.every(v => typeof v === 'string'))) throw new UnsupportedSchema(`${path}.required: must be an array of strings`);
  for (const key of ['enum', 'anyOf']) {
    if (Object.hasOwn(schema, key)) {
      const value = schema[key];
      if (!Array.isArray(value) || value.length === 0) throw new UnsupportedSchema(`${path}.${key}: must be a nonempty array`);
      if (key === 'anyOf') value.forEach((child, i) => checkSchema(child, `${path}.anyOf[${i}]`));
      else if (new Set(value.map(item => canonicalText(item))).size !== value.length) throw new UnsupportedSchema(`${path}.enum: duplicate value`);
    }
  }
  if (Object.hasOwn(schema, 'items')) checkSchema(schema.items, `${path}.items`);
  for (const [key, type, nonnegative] of [
    ['minimum', 'integer', false], ['maximum', 'integer', false], ['exclusiveMinimum', 'integer', false], ['exclusiveMaximum', 'integer', false],
    ['minLength', 'string', true], ['maxLength', 'string', true], ['minItems', 'array', true], ['maxItems', 'array', true],
  ] as const) {
    if (Object.hasOwn(schema, key)) {
      const value = schema[key];
      if (schema.type !== type || typeof value !== 'number' || !Number.isSafeInteger(value) || (nonnegative && value < 0)) throw new UnsupportedSchema(`${path}.${key}: requires ${type} type and ${nonnegative ? 'nonnegative ' : ''}integer bound`);
    }
  }
  for (const key of ['additionalProperties', 'sensitive']) if (Object.hasOwn(schema, key) && typeof schema[key] !== 'boolean') throw new UnsupportedSchema(`${path}.${key}: must be a boolean`);
  for (const key of ['title', 'description']) if (Object.hasOwn(schema, key) && typeof schema[key] !== 'string') throw new UnsupportedSchema(`${path}.${key}: must be a string`);
  if (schema.sensitive === true && !sensitiveString(schema)) throw new UnsupportedSchema(`${path}.sensitive: only string fields may be sensitive`);
}

function matchesType(value: IdentityValue, expected: string): boolean {
  switch (expected) {
    case 'object': return !!value && typeof value === 'object' && !Array.isArray(value);
    case 'array': return Array.isArray(value);
    case 'integer': return typeof value === 'number' && Number.isSafeInteger(value);
    case 'null': return value === null;
    default: return typeof value === expected;
  }
}

function validateValue(value: IdentityValue, schema: Schema, path: string): string | null {
  if (typeof schema.type === 'string' && !matchesType(value, schema.type)) return `${path}: expected ${schema.type}`;
  if (Array.isArray(schema.enum) && !schema.enum.some(item => canonicalText(item) === canonicalText(value))) return `${path}: value not in enum`;
  if (Array.isArray(schema.anyOf) && !schema.anyOf.some(child => validateValue(value, child as Schema, path) === null)) return `${path}: value does not match anyOf`;
  if (schema.type === 'integer' && typeof value === 'number') {
    if (typeof schema.minimum === 'number' && value < schema.minimum) return `${path}: below minimum`;
    if (typeof schema.maximum === 'number' && value > schema.maximum) return `${path}: above maximum`;
    if (typeof schema.exclusiveMinimum === 'number' && value <= schema.exclusiveMinimum) return `${path}: below exclusiveMinimum`;
    if (typeof schema.exclusiveMaximum === 'number' && value >= schema.exclusiveMaximum) return `${path}: above exclusiveMaximum`;
  }
  if (schema.type === 'string' && typeof value === 'string') {
    const length = Array.from(value).length;
    if (typeof schema.minLength === 'number' && length < schema.minLength) return `${path}: below minLength`;
    if (typeof schema.maxLength === 'number' && length > schema.maxLength) return `${path}: above maxLength`;
  }
  if (schema.type === 'array' && Array.isArray(value)) {
    if (typeof schema.minItems === 'number' && value.length < schema.minItems) return `${path}: below minItems`;
    if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) return `${path}: above maxItems`;
    if (schema.items) for (let i = 0; i < value.length; i++) {
      const finding = validateValue(value[i], schema.items as Schema, `${path}[${i}]`);
      if (finding) return finding;
    }
  }
  if (schema.type === 'object' || (schema.type === undefined && ['properties', 'required', 'additionalProperties'].some(key => Object.hasOwn(schema, key)))) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return `${path}: expected object`;
    if (Array.isArray(schema.required)) for (const key of schema.required as string[]) if (!Object.hasOwn(value, key)) return `${path}: missing required property ${key}`;
    const properties = (schema.properties ?? {}) as Record<string, Schema>;
    for (const [key, child] of Object.entries(properties)) if (Object.hasOwn(value, key)) {
      const finding = validateValue(value[key], child, `${path}.${key}`);
      if (finding) return finding;
    }
    if (schema.additionalProperties === false) for (const key of Object.keys(value).sort(codePointCompare)) if (!Object.hasOwn(properties, key)) return `${path}: unexpected property ${key}`;
  }
  return null;
}

function redactValue(value: IdentityValue, schema: Schema): IdentityValue {
  if (schema.sensitive === true && typeof value === 'string') return redact(value);
  if (Array.isArray(schema.anyOf)) {
    const branches = schema.anyOf as Schema[];
    const matching = branches.filter(child => validateValue(value, child, '$') === null);
    for (const branch of matching.length ? matching : branches) value = redactValue(value, branch);
  }
  if (Array.isArray(value) && schema.items) return value.map(item => redactValue(item, schema.items as Schema));
  if (value && typeof value === 'object' && !Array.isArray(value) && schema.properties) {
    const result = snapshot(value, true);
    for (const [key, child] of Object.entries(schema.properties as Record<string, Schema>)) if (Object.hasOwn(result, key)) result[key] = redactValue(result[key], child);
    return result;
  }
  return value;
}

export class ActionSpec {
  readonly name: string;
  readonly version: string;
  readonly description: string;
  readonly schema: Schema;
  readonly sideEffects: boolean;
  readonly handler?: ActionHandler;
  readonly specDigest: string;
  constructor(input: ActionSpecInput) {
    if (typeof input.name !== 'string' || !input.name || typeof input.version !== 'string' || !input.version || typeof input.description !== 'string' || typeof input.sideEffects !== 'boolean') throw new TypeError('invalid action spec');
    if (input.handler !== undefined && typeof input.handler !== 'function') throw new TypeError('handler must be a function');
    checkSchema(input.schema, `schema for ${input.name}`);
    this.name = input.name;
    this.version = input.version;
    this.description = input.description;
    this.schema = deepFreeze(snapshot(input.schema, true));
    this.sideEffects = input.sideEffects;
    this.handler = input.handler;
    this.specDigest = digestPayload({ name: this.name, version: this.version, description: this.description, schema: this.schema, side_effects: this.sideEffects });
    Object.freeze(this);
  }
  validateArgs(args: IdentityPayload): string | null {
    try { objectPayload(args, 'args'); } catch (error) { return error instanceof Error ? error.message : 'invalid args'; }
    return validateValue(args, this.schema, '$');
  }
  redactArgs(args: IdentityPayload): IdentityPayload {
    objectPayload(args, 'args');
    return snapshot(redactValue(args, this.schema) as IdentityPayload, true);
  }
}

export class Registry implements Iterable<ActionSpec> {
  #specs: Map<string, ActionSpec>;
  readonly registryDigest: string;
  constructor(specs: readonly ActionSpec[]) {
    this.#specs = new Map();
    for (const spec of specs) {
      if (!(spec instanceof ActionSpec)) throw new TypeError('registry entries must be ActionSpec instances');
      if (this.#specs.has(spec.name)) throw new TypeError(`duplicate action spec name: ${spec.name}`);
      this.#specs.set(spec.name, spec);
    }
    this.registryDigest = digestPayload({ spec_digests: specs.map(spec => spec.specDigest).sort() });
    Object.freeze(this);
  }
  get(name: string, version?: string): ActionSpec {
    const spec = this.#specs.get(name);
    if (!spec || (version !== undefined && version !== spec.version)) throw new Error(`unknown registered action: ${name}${version === undefined ? '' : `@${version}`}`);
    return spec;
  }
  [Symbol.iterator](): Iterator<ActionSpec> { return this.#specs.values(); }
}

export type Decision = 'allow' | 'deny' | 'confirm';
export interface PolicyContext {
  readonly spec: ActionSpec;
  readonly args: IdentityPayload;
  readonly cursorId: string;
  readonly runLabel: string;
  readonly counters: { steps: number; tokens: number };
}
export interface Policy { decide(context: PolicyContext): Decision; }
