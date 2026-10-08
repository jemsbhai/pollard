#!/usr/bin/env node
import { writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { AtomicStore, SQLiteStore } from './stores.js';
import { PostgresStore, RedisStore, MongoStore, Neo4jStore, KafkaStore } from './remote.js';
import { Node, Store, verify } from './tree.js';
import { exportJSONL, exportSubtree, gc, importJSONL, importSubtree, merge } from './governance.js';
import { seal } from './seal.js';
import { addCharges, chargeAmount } from './meters.js';

const help = `pollardai — inspect and maintain Pollard SQLite recordings offline

  pollardai runs DB [DB ...] [--json]
  pollardai show DB ROOT [--json] [--unicode] [--include-payloads] [--html FILE]
  pollardai report DB ROOT [--json]
  pollardai verify DB [ROOT] [--json]
  pollardai seal DB ROOT [--json] [--output FILE]
  pollardai export DB ROOT FILE [--format json|jsonl]
  pollardai import FILE DB [--format json|jsonl]
  pollardai merge --into DB SOURCE [SOURCE ...] [--replay] [--json]
  pollardai gc DB --mode drop-pruned|compact [--json]

Read commands open existing stores without creation. Install the optional driver.
Remote specs: pg-env:DSN#STORE, redis-env:URL?prefix=pollard#STORE,
mongo-env:URI?database=pollard&prefix=pollard_npm#STORE,
neo4j-env:URI?user-env=USER&password-env=PASSWORD&database=neo4j#STORE,
kafka-env:CONFIG?topic=TOPIC#STORE (CONFIG contains KafkaJS JSON with brokers).
`;
type ClosableStore = Store & { close(): void };
function variable(name: string): string {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new TypeError('invalid environment variable name in store specification');
  const value = process.env[name]; if (!value) throw new TypeError(`store environment variable is unset: ${name}`); return value;
}
function open(path: string, readOnly: boolean): ClosableStore {
  if (!path || /[\x00-\x1f]/.test(path)) throw new TypeError('invalid store path');
  const match = /^(pg|redis|mongo|neo4j|kafka)-env:([^?#]+)(?:\?([^#]*))?(?:#(.*))?$/.exec(path);
  if (match) {
    const [, backend, name, query = '', fragment = 'default'] = match;
    if (/%(?![a-fA-F0-9]{2})/.test(query + fragment)) throw new TypeError('invalid percent encoding in store specification');
    // URLSearchParams replaces invalid UTF-8 with U+FFFD; reject malformed
    // encodings before they can select or create a different namespace.
    decodeURIComponent(query.replace(/\+/g, ' '));
    const parameters = new URLSearchParams(query), seen = new Set<string>(), storeId = decodeURIComponent(fragment);
    if (!storeId || /[\x00-\x1f?#]/.test(storeId)) throw new TypeError('invalid store id');
    const allowed: Record<string, string[]> = { pg: [], redis: ['prefix'], mongo: ['database', 'prefix'], neo4j: ['database', 'user-env', 'password-env'], kafka: ['topic', 'timeout'] };
    for (const [key, value] of parameters) { if (seen.has(key) || !allowed[backend].includes(key) || !value || /[\x00-\x1f]/.test(value)) throw new TypeError('invalid store query parameter'); seen.add(key); }
    const url = variable(name), config = { storeId, create: !readOnly };
    try {
      if (backend === 'pg') return new PostgresStore(url, config);
      if (backend === 'redis') return new RedisStore(url, { ...config, prefix: parameters.get('prefix') ?? 'pollard' });
      if (backend === 'mongo') return new MongoStore(url, { ...config, database: parameters.get('database') ?? 'pollard', prefix: parameters.get('prefix') ?? 'pollard_npm' });
      if (backend === 'neo4j') return new Neo4jStore(url, { ...config, database: parameters.get('database') ?? 'neo4j', username: variable(parameters.get('user-env') ?? ''), password: variable(parameters.get('password-env') ?? '') });
      const client = JSON.parse(url), topic = parameters.get('topic');
      if (!client || typeof client !== 'object' || Array.isArray(client) || !Array.isArray(client.brokers) || !topic) throw new TypeError('Kafka config requires brokers and the store spec requires topic');
      const seconds = Number(parameters.get('timeout') ?? '30'); if (!Number.isSafeInteger(seconds) || seconds <= 0) throw new TypeError('Kafka timeout must be a positive integer');
      return new KafkaStore({ ...config, clientConfig: client, brokers: client.brokers, topic, timeoutMs: seconds * 1000, readOnly });
    } catch (error) { throw new Error(`unable to open ${backend} store (${error instanceof Error ? error.name : 'Error'}); check the environment configuration and service`); }
  }
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path) || /^(?:pg|postgres|redis|mongo|neo4j|bolt|kafka)[^:]*:/i.test(path)) throw new TypeError('remote CLI connections require an environment-backed store specification');
  return new SQLiteStore(path, { readOnly });
}
function sum(store: Store, root: string, key: string): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const node of store.walk(root)) {
    const values = node.meta[key];
    if (values && typeof values === 'object' && !Array.isArray(values)) for (const [name, value] of Object.entries(values)) Object.defineProperty(totals, name, { value: addCharges(Object.hasOwn(totals, name) ? totals[name] : 0, chargeAmount(value, `stored ${name}`)), enumerable: true, writable: true, configurable: true });
  }
  return totals;
}
function summary(node: Node): Record<string, unknown> {
  return { id: node.id, parent: node.parent, kind: node.kind, attempt: node.attempt, label: node.kind === 'root' ? node.payload.run : node.kind === 'tool_call' ? node.payload.tool : node.kind === 'model_call' ? node.payload.model ?? node.payload.modelId : undefined, charges: node.meta.charges ?? {}, pruned: node.meta.pruned === true };
}
function tree(store: Store, root: string, includePayloads: boolean, unicode: boolean): { nodes: Record<string, unknown>[]; text: string } {
  const rows: Record<string, unknown>[] = [], lines: string[] = [], depths = new Map<string, number>();
  for (const node of store.walk(root)) {
    const depth = node.id === root ? 0 : (depths.get(node.parent!) ?? -1) + 1; depths.set(node.id, depth);
    const row = summary(node); if (includePayloads) { row.payload = node.payload; row.result = node.result; }
    rows.push(row); lines.push(`${'  '.repeat(depth)}${depth ? unicode ? '└─ ' : '+- ' : ''}${node.kind} ${String(row.label ?? '')} ${node.id}${row.pruned ? ' [pruned]' : ''}`);
  }
  return { nodes: rows, text: lines.join('\n') };
}
const escaped = (value: string): string => value.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!);
function emit(value: unknown): void { process.stdout.write(JSON.stringify(value, null, 2) + '\n'); }
export function main(args: string[] = process.argv.slice(2)): number {
  const opened: ClosableStore[] = [];
  try {
    const parsed = parseArgs({ args, allowPositionals: true, options: {
      help: { type: 'boolean', short: 'h' }, json: { type: 'boolean' }, unicode: { type: 'boolean' }, 'include-payloads': { type: 'boolean' },
      html: { type: 'string' }, output: { type: 'string' }, format: { type: 'string' }, into: { type: 'string' }, mode: { type: 'string' }, replay: { type: 'boolean' },
    } });
    const [command, ...rest] = parsed.positionals, options = parsed.values;
    if (options.help || !command) { process.stdout.write(help); return 0; }
    const use = (path: string, readOnly = true): ClosableStore => { const store = open(path, readOnly); opened.push(store); return store; };
    const count = (min: number, max = min): void => { if (rest.length < min || rest.length > max) throw new TypeError(`invalid arguments for ${command}; use --help`); };
    const format = options.format ?? 'json'; if (!['json', 'jsonl'].includes(format)) throw new TypeError('format must be json or jsonl');
    switch (command) {
      case 'runs': {
        count(1, Number.MAX_SAFE_INTEGER);
        emit(rest.flatMap(path => { const store = use(path); return store.roots().map(root => ({ store: path, ...summary(store.get(root)) })); })); return 0;
      }
      case 'show': {
        count(2); const document = tree(use(rest[0]), rest[1], options['include-payloads'] ?? false, options.unicode ?? false);
        if (options.html) writeFileSync(options.html, `<!doctype html><html lang="en"><meta charset="utf-8"><title>Pollard execution tree</title><style>body{font:15px system-ui;margin:2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Pollard execution tree</h1><pre>${escaped(options['include-payloads'] ? JSON.stringify(document.nodes, null, 2) : document.text)}</pre></html>`, 'utf8');
        if (options.json) emit({ root_id: rest[1], nodes: document.nodes }); else process.stdout.write(document.text + '\n'); return 0;
      }
      case 'report': { count(2); const store = use(rest[0]); emit({ root_id: rest[1], spent: sum(store, rest[1], 'charges'), avoided: sum(store, rest[1], 'avoided') }); return 0; }
      case 'verify': {
        count(1, 2); const store = use(rest[0]), findings: unknown[] = [];
        for (const root of rest[1] ? [rest[1]] : store.roots()) for (const node of store.walk(root)) findings.push(...verify(store, node.id).findings);
        emit({ ok: findings.length === 0, findings }); return findings.length ? 1 : 0;
      }
      case 'seal': { count(2); const report = seal(use(rest[0]), rest[1]); if (options.output) writeFileSync(options.output, JSON.stringify(report, null, 2) + '\n', 'utf8'); emit(report); return 0; }
      case 'export': { count(3); emit((format === 'jsonl' ? exportJSONL : exportSubtree)(use(rest[0]), rest[1], rest[2])); return 0; }
      case 'import': { count(2); emit((format === 'jsonl' ? importJSONL : importSubtree)(rest[0], use(rest[1], false))); return 0; }
      case 'merge': {
        count(1, Number.MAX_SAFE_INTEGER); if (!options.into) throw new TypeError('merge requires --into DB');
        // Open and validate all sources before creating or mutating the destination.
        const sources = rest.map(path => use(path)); for (const source of sources) for (const root of source.roots()) seal(source, root);
        const destination = use(options.into, false);
        const atomic = destination as ClosableStore & Partial<AtomicStore>;
        if (typeof atomic.transaction !== 'function') throw new TypeError('merge destination must support atomic transactions');
        emit(atomic.transaction(() => sources.map(source => merge(destination, source, { replay: options.replay, requireAtomic: true })))); return 0;
      }
      case 'gc': { count(1); if (options.mode !== 'drop-pruned' && options.mode !== 'compact') throw new TypeError('gc requires --mode drop-pruned or --mode compact'); if (/^[a-z]+-env:/i.test(rest[0])) throw new TypeError('CLI gc requires a local SQLite path'); emit(gc(use(rest[0], false), { mode: options.mode })); return 0; }
      default: throw new TypeError('unknown command; use --help');
    }
  } catch (error) {
    const remote = args.some(arg => /(?:^|=)(?:pg|redis|mongo|neo4j|kafka)-env:/i.test(arg) || /[a-z][a-z0-9+.-]*:\/\//i.test(arg));
    process.stderr.write(`pollardai: ${remote ? `remote store operation failed (${error instanceof Error ? error.name : 'Error'}); check configuration and store integrity` : error instanceof Error ? error.message : 'operation failed'}\n`); return 1;
  } finally { for (const store of opened.reverse()) try { store.close(); } catch { /* Preserve the operation's primary error or successful output. */ } }
}
process.exitCode = main();
