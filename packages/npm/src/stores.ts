import { loadSQLiteDriver } from './sqlite-driver.cjs';
import { canonicalText, codePointCompare, IdentityPayload, IdentityValue, JsonObject, resultToText, sha256, snapshot } from './identity.js';
import { IntegrityError, MissingNodeError, Node, NodeKind, RecordingStore, Store, validateNode } from './tree.js';

export interface BudgetReservation { scopeId: string; limits: Record<string, number>; baseline: Record<string, number>; estimates: Record<string, number>; }
export interface WindowReservation { ledgerKey: string; meter: string; limit: number; amount: number; windowSeconds: number; }
export interface ReservationCheck { ok: boolean; reason?: 'budget' | 'window'; meter?: string; requested?: number; remaining?: number; windowSeconds?: number; }
export interface TransactionalArbiter {
  pollardReserve(id: string, budgets: BudgetReservation[], windows: WindowReservation[], leaseSeconds: number): ReservationCheck;
  pollardSettle(id: string, charges: Record<string, number>): void;
  pollardRelease(id: string): void;
  pollardRenew(id: string, leaseSeconds: number): boolean;
}
export interface AtomicStore extends Store { transaction<T>(operation: () => T): T; }
export interface MaintenanceStore extends Store { dropNodes(ids: ReadonlySet<string>): void; compact(): number; }
type Row = Record<string, unknown>;
export interface SQLiteDatabase {
  exec(sql: string): unknown;
  prepare(sql: string): { get(...values: unknown[]): Row | undefined; all(...values: unknown[]): Row[]; run(...values: unknown[]): { changes: number; lastInsertRowid: number | bigint } };
  close(): void;
}
export type SQLiteDriver = new (path: string, options?: { readonly?: boolean; fileMustExist?: boolean; timeout?: number }) => SQLiteDatabase;
export function openSQLite(path: string, readOnly = false, driver?: SQLiteDriver): SQLiteDatabase {
  const Driver = driver ?? loadSQLiteDriver() as SQLiteDriver;
  return new Driver(path, { readonly: readOnly, fileMustExist: readOnly, timeout: 30_000 });
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, parent TEXT, kind TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL, result TEXT, result_digest TEXT, meta TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs (digest TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blob_literals (node_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(node_id,path));
CREATE TABLE IF NOT EXISTS budget_state (scope_id TEXT NOT NULL, meter TEXT NOT NULL, settled TEXT NOT NULL, PRIMARY KEY(scope_id,meter));
CREATE TABLE IF NOT EXISTS reservations (reservation_id TEXT NOT NULL, kind TEXT NOT NULL, scope_id TEXT NOT NULL, meter TEXT NOT NULL, amount TEXT NOT NULL, expires_at REAL NOT NULL, window_seconds REAL, PRIMARY KEY(reservation_id,kind,scope_id,meter));
CREATE INDEX IF NOT EXISTS idx_reservations_scope ON reservations(kind,scope_id,meter,expires_at);
CREATE TABLE IF NOT EXISTS window_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, meter TEXT NOT NULL, amount TEXT NOT NULL, settled_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_window_events_scope ON window_events(scope_id,meter,settled_at);
`;
const blobDigest = (v: IdentityValue): string | undefined => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 1 && typeof v.__pollard_ref === 'string' && /^[a-f0-9]{64}$/.test(v.__pollard_ref) ? v.__pollard_ref : undefined;
const pathText = (path: (string | number)[]): string => JSON.stringify(path);
function mapPayload(value: IdentityValue, visit: (value: IdentityValue, path: (string | number)[]) => IdentityValue | undefined, path: (string | number)[] = []): IdentityValue {
  const mapped = visit(value, path);
  if (mapped !== undefined) return mapped;
  if (Array.isArray(value)) return value.map((v, i) => mapPayload(v, visit, [...path, i]));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, mapPayload(v, visit, [...path, k])]));
  return value;
}

export interface SQLiteStoreOptions { internPayloads?: boolean; internThreshold?: number; readOnly?: boolean; driver?: SQLiteDriver; }
/** Synchronous, transactional SQLite store interoperable with Python schema v3. */
export class SQLiteStore implements RecordingStore, AtomicStore, MaintenanceStore, TransactionalArbiter {
  readonly path: string;
  readonly readOnly: boolean;
  readonly internPayloads: boolean;
  readonly internThreshold: number;
  readonly #db: SQLiteDatabase;
  #depth = 0;
  #finalizable = new Set<string>();
  constructor(path: string, options: SQLiteStoreOptions = {}) {
    this.path = path; this.readOnly = options.readOnly ?? false; this.internPayloads = options.internPayloads ?? true; this.internThreshold = options.internThreshold ?? 1024;
    if (typeof path !== 'string' || !path) throw new TypeError('path must be a nonempty string');
    if (typeof this.readOnly !== 'boolean' || typeof this.internPayloads !== 'boolean') throw new TypeError('SQLite boolean options must be booleans');
    if (!Number.isSafeInteger(this.internThreshold) || this.internThreshold < 1) throw new TypeError('internThreshold must be a positive integer');
    this.#db = openSQLite(path, this.readOnly, options.driver);
    try {
      this.#db.exec('PRAGMA busy_timeout=30000');
      const tables = new Set(this.#db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => r.name));
      const version = tables.has('kv') ? this.#db.prepare("SELECT v FROM kv WHERE k='schema_version'").get()?.v : undefined;
      if (version !== undefined && (!/^[0-3]$/.test(String(version)))) throw new IntegrityError(`unsupported SQLite schema version: ${version}`);
      if (this.readOnly) {
        if (String(version) !== '3' || ['nodes','kv','blobs','blob_literals'].some(t => !tables.has(t))) throw new IntegrityError('read-only replay requires complete SQLite schema version 3; open a writable copy once to migrate it');
        this.#db.exec('PRAGMA query_only=ON');
      } else {
        this.#db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL');
        this.transaction(() => {
          this.#db.exec(SCHEMA);
          if (Number(version ?? 0) < 2) {
            for (const row of this.#db.prepare('SELECT id,payload FROM nodes').all()) mapPayload(JSON.parse(String(row.payload)), (v, p) => {
              if (blobDigest(v)) { this.#db.prepare('INSERT OR IGNORE INTO blob_literals(node_id,path) VALUES(?,?)').run(row.id, pathText(p)); return v; }
              return undefined;
            });
          }
          this.#db.prepare("INSERT OR REPLACE INTO kv(k,v) VALUES('schema_version','3')").run();
        });
      }
    } catch (error) { this.#db.close(); throw error; }
  }
  close(): void { this.#db.close(); }
  transaction<T>(operation: () => T): T {
    const outer = this.#depth === 0;
    const point = `pollard_${this.#depth}`;
    const finalizable = new Set(this.#finalizable);
    this.#db.exec(outer ? 'BEGIN IMMEDIATE' : `SAVEPOINT ${point}`); this.#depth++;
    try {
      const result = operation();
      if (result && typeof (result as { then?: unknown }).then === 'function') throw new TypeError('store transactions must be synchronous');
      this.#db.exec(outer ? 'COMMIT' : `RELEASE ${point}`);
      return result;
    } catch (error) {
      this.#finalizable = finalizable;
      this.#db.exec(outer ? 'ROLLBACK' : `ROLLBACK TO ${point}; RELEASE ${point}`);
      throw error;
    } finally { this.#depth--; }
  }
  put(node: Node): void {
    validateNode(node);
    this.transaction(() => {
      if (node.parent !== null && !this.exists(node.parent)) throw new MissingNodeError(node.parent);
      if (this.exists(node.id)) {
        const existing = this.get(node.id);
        if (!sameIdentity(existing, node)) throw new IntegrityError('node id collision');
        if (node.resultText !== null && node.resultText !== existing.resultText) {
          const conflicts = Array.isArray(existing.meta.result_conflicts) ? existing.meta.result_conflicts : [];
          this.updateMeta(node.id, { result_conflicts: [...conflicts, { result_digest: node.resultDigest, result: node.result }] });
        }
        return;
      }
      const literals: string[] = [];
      const payload = mapPayload(node.payload, (value, path) => {
        if (blobDigest(value)) { literals.push(pathText(path)); return value; }
        if (this.internPayloads && typeof value === 'string' && Buffer.byteLength(value, 'utf8') >= this.internThreshold) {
          const digest = sha256(value), existing = this.#db.prepare('SELECT value FROM blobs WHERE digest=?').get(digest);
          if (existing && existing.value !== value) throw new IntegrityError('blob digest collision');
          this.#db.prepare('INSERT OR IGNORE INTO blobs(digest,value) VALUES(?,?)').run(digest, value);
          return { __pollard_ref: digest };
        }
        return undefined;
      });
      this.#db.prepare('INSERT INTO nodes(id,parent,kind,attempt,payload,result,result_digest,meta) VALUES(?,?,?,?,?,?,?,?)').run(node.id,node.parent,node.kind,node.attempt,canonicalText(payload),node.resultText,node.resultDigest,resultToText(node.meta));
      for (const path of literals) this.#db.prepare('INSERT INTO blob_literals(node_id,path) VALUES(?,?)').run(node.id,path);
      if (node.resultText === null && node.meta.state === 'pending') this.#finalizable.add(node.id);
    });
  }
  /** Atomically acquire dispatch ownership across connections and processes. */
  claim(node: Node): boolean {
    validateNode(node);
    if (node.resultText !== null || node.meta.state !== 'pending') throw new IntegrityError('dispatch claim requires a pending node');
    return this.transaction(() => { if (this.exists(node.id)) return false; this.put(node); return true; });
  }
  get(id: string): Node {
    const row = this.#db.prepare('SELECT * FROM nodes WHERE id=?').get(id);
    if (!row) throw new MissingNodeError(id);
    const literals = this.#literals(id);
    const payload = mapPayload(JSON.parse(String(row.payload)), (value, path) => {
      const digest = blobDigest(value);
      if (digest && !literals.has(pathText(path))) {
        const blob = this.#db.prepare('SELECT value FROM blobs WHERE digest=?').get(digest);
        if (!blob || typeof blob.value !== 'string' || sha256(blob.value) !== digest) throw new IntegrityError(`missing or corrupt interned payload blob: ${digest}`);
        return blob.value;
      }
      return digest ? value : undefined;
    }) as IdentityPayload;
    return Node.fromStorage({ id: String(row.id), parent: row.parent === null ? null : String(row.parent), kind: row.kind as NodeKind, attempt: Number(row.attempt), payload, result_text: row.result === null ? null : String(row.result), result_digest: row.result_digest === null ? null : String(row.result_digest), meta: JSON.parse(String(row.meta)) });
  }
  exists(id: string): boolean { return !!this.#db.prepare('SELECT 1 FROM nodes WHERE id=?').get(id); }
  children(id: string): string[] { return this.#db.prepare('SELECT id FROM nodes WHERE parent=? ORDER BY kind,id').all(id).map(r => String(r.id)); }
  updateMeta(id: string, patch: JsonObject): void {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new TypeError('meta patch must be an object');
    const copy = snapshot(patch);
    this.transaction(() => {
      const node = this.get(id);
      this.#db.prepare('UPDATE nodes SET meta=? WHERE id=?').run(resultToText({ ...node.meta, ...copy }), id);
      if (Object.hasOwn(copy, 'state') && copy.state !== 'pending') this.#finalizable.delete(id);
    });
  }
  *walk(rootId: string): Iterable<Node> {
    const pending = [rootId], seen = new Set<string>();
    while (pending.length) {
      const id = pending.pop()!;
      if (seen.has(id)) throw new IntegrityError('cycle in store traversal');
      seen.add(id); yield this.get(id); pending.push(...this.children(id).reverse());
    }
  }
  roots(): string[] { return this.#db.prepare('SELECT id FROM nodes WHERE parent IS NULL').all().map(r => String(r.id)).sort((a,b) => codePointCompare(String(this.get(a).payload.run ?? ''),String(this.get(b).payload.run ?? '')) || codePointCompare(a,b)); }
  finalize(node: Node): void {
    validateNode(node);
    this.transaction(() => {
      const old = this.get(node.id);
      if (!this.#finalizable.has(node.id) || old.resultText !== null || old.meta.state !== 'pending' || node.meta.state !== 'completed' || !sameIdentity(old,node)) throw new IntegrityError('only a pending dispatch can be finalized once');
      this.#db.prepare('UPDATE nodes SET result=?,result_digest=?,meta=? WHERE id=?').run(node.resultText,node.resultDigest,resultToText(node.meta),node.id);
      this.#finalizable.delete(node.id);
    });
  }
  dropNodes(ids: ReadonlySet<string>): void {
    this.transaction(() => {
      for (const id of ids) if (this.children(id).some(child => !ids.has(child))) throw new IntegrityError('cannot remove a parent while retaining its children');
      for (const id of ids) { this.#db.prepare('DELETE FROM blob_literals WHERE node_id=?').run(id); this.#db.prepare('DELETE FROM nodes WHERE id=?').run(id); this.#finalizable.delete(id); }
    });
  }
  compact(): number {
    const count = this.transaction(() => {
      const used = new Set<string>();
      for (const row of this.#db.prepare('SELECT id,payload FROM nodes').all()) {
        const literals = this.#literals(String(row.id));
        mapPayload(JSON.parse(String(row.payload)), (value,path) => { const digest = blobDigest(value); if (digest) { if (!literals.has(pathText(path))) used.add(digest); return value; } return undefined; });
      }
      let removed = 0;
      for (const row of this.#db.prepare('SELECT digest FROM blobs').all()) if (!used.has(String(row.digest))) { this.#db.prepare('DELETE FROM blobs WHERE digest=?').run(row.digest); removed++; }
      this.#db.prepare('DELETE FROM reservations WHERE expires_at<=?').run(Date.now()/1000);
      return removed;
    });
    if (this.#depth === 0) this.#db.exec('VACUUM');
    return count;
  }
  #literals(id: string): Set<string> { return new Set(this.#db.prepare('SELECT path FROM blob_literals WHERE node_id=?').all(id).map(r => String(r.path))); }
  pollardReserve(id: string, budgets: BudgetReservation[], windows: WindowReservation[], leaseSeconds: number): ReservationCheck {
    if (typeof id !== 'string' || !id) throw new TypeError('reservation id is required');
    positive(leaseSeconds, 'leaseSeconds');
    return this.transaction(() => {
      const now = Date.now()/1000;
      if (this.#db.prepare('SELECT 1 FROM reservations WHERE reservation_id=?').get(id)) throw new IntegrityError('duplicate reservation id');
      for (const request of budgets) for (const [meter, limit] of Object.entries(request.limits)) {
        if (meter === 'depth') continue;
        amount(limit); amount(meterAmount(request.baseline, meter)); amount(meterAmount(request.estimates, meter));
        const baseline = String(meterAmount(request.baseline, meter));
        this.#db.prepare('INSERT OR IGNORE INTO budget_state(scope_id,meter,settled) VALUES(?,?,?)').run(request.scopeId,meter,baseline);
        let settled = String(this.#db.prepare('SELECT settled FROM budget_state WHERE scope_id=? AND meter=?').get(request.scopeId,meter)!.settled);
        if (decimalCompare(baseline,settled)>0) { settled=baseline; this.#db.prepare('UPDATE budget_state SET settled=? WHERE scope_id=? AND meter=?').run(settled,request.scopeId,meter); }
        const active = this.#db.prepare("SELECT amount FROM reservations WHERE kind='budget' AND scope_id=? AND meter=? AND expires_at>?").all(request.scopeId,meter,now).reduce((a,r)=>decimalAdd(a,String(r.amount)),'0');
        const remaining=decimalAdd(decimalAdd(String(limit),settled,true),active,true), requested=meterAmount(request.estimates,meter);
        if(decimalCompare(String(requested),remaining)>0) return {ok:false,reason:'budget',meter,requested,remaining:Number(remaining)};
      }
      for (const request of windows) {
        amount(request.limit); amount(request.amount); positive(request.windowSeconds,'windowSeconds');
        const cutoff=now-request.windowSeconds;
        this.#db.prepare('DELETE FROM window_events WHERE scope_id=? AND settled_at<=?').run(request.ledgerKey,cutoff);
        const settled=this.#db.prepare('SELECT amount FROM window_events WHERE scope_id=? AND settled_at>?').all(request.ledgerKey,cutoff).reduce((a,r)=>decimalAdd(a,String(r.amount)),'0');
        const active=this.#db.prepare("SELECT amount FROM reservations WHERE kind='window' AND scope_id=? AND expires_at>?").all(request.ledgerKey,now).reduce((a,r)=>decimalAdd(a,String(r.amount)),'0');
        const remaining=decimalAdd(decimalAdd(String(request.limit),settled,true),active,true);
        if(decimalCompare(String(request.amount),remaining)>0) return {ok:false,reason:'window',meter:request.meter,requested:request.amount,remaining:Number(remaining),windowSeconds:request.windowSeconds};
      }
      const expiresAt=this.path===':memory:'?Infinity:now+leaseSeconds;
      for(const request of budgets) for(const meter of Object.keys(request.limits)) if(meter!=='depth') this.#db.prepare("INSERT INTO reservations VALUES(?,'budget',?,?,?,?,NULL)").run(id,request.scopeId,meter,String(meterAmount(request.estimates,meter)),expiresAt);
      for(const request of windows) this.#db.prepare("INSERT INTO reservations VALUES(?,'window',?,?,?,?,?)").run(id,request.ledgerKey,request.meter,String(request.amount),expiresAt,request.windowSeconds);
      return {ok:true};
    });
  }
  pollardSettle(id: string, charges: Record<string,number>): void {
    for(const value of Object.values(charges)) amount(value);
    this.transaction(() => {
      const now=Date.now()/1000;
      for(const row of this.#db.prepare('SELECT * FROM reservations WHERE reservation_id=?').all(id)) {
        const actual=String(meterAmount(charges,String(row.meter)));
        if(row.kind==='budget') {
          const state=this.#db.prepare('SELECT settled FROM budget_state WHERE scope_id=? AND meter=?').get(row.scope_id,row.meter);
          if(!state) throw new IntegrityError('budget state missing during settlement');
          this.#db.prepare('UPDATE budget_state SET settled=? WHERE scope_id=? AND meter=?').run(decimalAdd(String(state.settled),actual),row.scope_id,row.meter);
        } else if(Number(actual)!==0) this.#db.prepare('INSERT INTO window_events(scope_id,meter,amount,settled_at) VALUES(?,?,?,?)').run(row.scope_id,row.meter,actual,now);
      }
      this.#db.prepare('DELETE FROM reservations WHERE reservation_id=?').run(id);
    });
  }
  pollardRelease(id: string): void { this.#db.prepare('DELETE FROM reservations WHERE reservation_id=?').run(id); }
  pollardRenew(id: string, leaseSeconds: number): boolean {
    positive(leaseSeconds,'leaseSeconds');
    return this.transaction(() => {
      const now=Date.now()/1000, rows=this.#db.prepare('SELECT expires_at FROM reservations WHERE reservation_id=?').all(id);
      if(!rows.length || rows.some(row=>Number(row.expires_at)<=now)) return false;
      this.#db.prepare('UPDATE reservations SET expires_at=? WHERE reservation_id=?').run(this.path===':memory:'?Infinity:now+leaseSeconds,id); return true;
    });
  }
}
export function sameIdentity(a: Node, b: Node): boolean { return a.id===b.id && a.kind===b.kind && a.parent===b.parent && a.attempt===b.attempt && canonicalText(a.payload)===canonicalText(b.payload); }
function amount(value: number): void { if(typeof value!=='number' || !Number.isFinite(value) || value<0 || value>Number.MAX_SAFE_INTEGER) throw new TypeError('meter amount must be a nonnegative finite safe number'); }
function meterAmount(values: Record<string,number>, meter: string): number { return Object.hasOwn(values,meter) ? values[meter] : 0; }
function positive(value: number, name: string): void { amount(value); if(value===0) throw new TypeError(`${name} must be positive`); }
// Decimal arithmetic preserves Python's persisted Decimal ledgers (including 0.1 + 0.2).
function decimal(value: string): [bigint,number] {
  const match=/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(value);
  if(!match) throw new IntegrityError('invalid persisted decimal amount');
  const scale=(match[3]?.length??0)-Number(match[4]??0);
  if(Math.abs(scale)>10000) throw new IntegrityError('decimal exponent is out of range');
  let coefficient=BigInt((match[1]==='-'?'-':'')+match[2]+(match[3]??''));
  if(scale<0) coefficient*=10n**BigInt(-scale);
  return [coefficient,Math.max(0,scale)];
}
export function decimalAdd(a:string,b:string,subtract=false):string {
  const [aa,as]=decimal(a),[bb,bs]=decimal(b),scale=Math.max(as,bs),sum=aa*10n**BigInt(scale-as)+(subtract?-1n:1n)*bb*10n**BigInt(scale-bs);
  const sign=sum<0n?'-':'',digits=(sum<0n?-sum:sum).toString().padStart(scale+1,'0');
  return scale?sign+digits.slice(0,-scale)+'.'+digits.slice(-scale):sign+digits;
}
export function decimalCompare(a:string,b:string):number { const [difference]=decimal(decimalAdd(a,b,true)); return difference<0n?-1:difference>0n?1:0; }
