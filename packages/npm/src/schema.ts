import { IdentityPayload, IdentityValue, objectPayload, snapshot } from './identity.js';
import { UnsupportedSchema } from './tree.js';

function object(value: IdentityValue | undefined): value is IdentityPayload {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Detect references only in schema positions, preserving literal defaults/enums. */
export function schemaHasLocalRefs(schema: IdentityPayload): boolean {
  const pending: IdentityValue[] = [schema];
  while (pending.length) {
    const value = pending.pop();
    if (!object(value)) continue;
    if (['$ref', '$defs', 'definitions'].some(key => Object.hasOwn(value, key))) return true;
    if (object(value.properties)) pending.push(...Object.values(value.properties));
    if (Array.isArray(value.anyOf)) pending.push(...value.anyOf);
    if (Object.hasOwn(value, 'items')) pending.push(value.items);
  }
  return false;
}

/** Expand finite local JSON Pointer references before validating and digesting a schema. */
export function resolveLocalRefs(schema: IdentityPayload): IdentityPayload {
  try { objectPayload(schema, 'schema'); }
  catch (error) { throw new UnsupportedSchema(error instanceof Error ? error.message : 'invalid schema'); }
  const root = snapshot(schema, true);
  const resolve = (value: IdentityValue, path: string, active: readonly string[]): IdentityValue => {
    if (!object(value)) return value;
    if (Object.hasOwn(value, '$ref')) {
      const reference = value.$ref;
      if (typeof reference !== 'string') throw new UnsupportedSchema(`${path}.$ref: must be a string`);
      const siblings = Object.keys(value).filter(key => !['$defs', '$ref', 'default', 'definitions', 'description', 'sensitive', 'title'].includes(key));
      if (siblings.length) throw new UnsupportedSchema(`${path}.$ref: unsupported sibling keywords ${siblings.join(', ')}`);
      if (reference !== '#' && !reference.startsWith('#/')) throw new UnsupportedSchema(`${path}.$ref: only local JSON Pointer references are supported`);
      let pointer: string;
      try { pointer = decodeURIComponent(reference.slice(1)); }
      catch { throw new UnsupportedSchema(`${path}.$ref: invalid percent escape`); }
      if (active.includes(pointer)) throw new UnsupportedSchema(`${path}.$ref: cyclic local reference ${[...active, pointer].join(' -> ')}`);
      let target: IdentityValue = root;
      if (pointer) for (const raw of pointer.slice(1).split('/')) {
        if (/~(?:[^01]|$)/.test(raw)) throw new UnsupportedSchema(`${path}.$ref: invalid JSON Pointer escape`);
        const token = raw.replace(/~1/g, '/').replace(/~0/g, '~');
        if (object(target) && Object.hasOwn(target, token)) target = target[token];
        else if (Array.isArray(target) && /^\d+$/.test(token) && Number(token) < target.length) target = target[Number(token)];
        else throw new UnsupportedSchema(`${path}.$ref: missing local reference target`);
      }
      const expanded = resolve(target, `reference ${reference}`, [...active, pointer]);
      if (!object(expanded)) throw new UnsupportedSchema(`${path}.$ref: target must be a schema object`);
      const combined = { ...expanded };
      for (const [key, child] of Object.entries(value)) if (!['$defs', '$ref', 'definitions'].includes(key)) {
        Object.defineProperty(combined, key, { value: snapshot(child, true), enumerable: true, configurable: true, writable: true });
      }
      return combined;
    }
    const children: IdentityPayload = {};
    for (const [key, child] of Object.entries(value)) {
      if (key === '$defs' || key === 'definitions') continue;
      let resolved: IdentityValue;
      if (key === 'properties' && object(child)) resolved = Object.fromEntries(Object.entries(child).map(([name, item]) => [name, resolve(item, `${path}.properties.${name}`, active)]));
      else if (key === 'anyOf' && Array.isArray(child)) resolved = child.map((item, index) => resolve(item, `${path}.anyOf[${index}]`, active));
      else if (key === 'items') resolved = resolve(child, `${path}.items`, active);
      else resolved = snapshot(child, true);
      Object.defineProperty(children, key, { value: resolved, enumerable: true, configurable: true, writable: true });
    }
    return children;
  };
  return resolve(root, '$', []) as IdentityPayload;
}
