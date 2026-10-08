import { canonicalText, deepFreeze, resultToText, sha256 } from './identity.js';
import { IntegrityError, Store, validateNode } from './tree.js';
import { openSQLite, SQLiteDatabase, SQLiteDriver } from './stores.js';

export const SEAL_DOMAIN = 'pollard/v1:seal\n';
export const SEAL_ALGORITHM = 'sha256:pollard/v1:seal';
export interface SealEntry { index: number; node_id: string; parent_id: string | null; kind: string; result_digest: string | null; previous: string | null; seal: string; }
/** Wire keys deliberately match Python's seal report and subtree export format. */
export interface SealReport { root_id: string; algorithm: string; digest: string; entries: readonly SealEntry[]; }
export function seal(store: Store, rootId: string): SealReport {
  const entries: SealEntry[] = [], seen = new Set<string>();
  let previous = '';
  for (const node of store.walk(rootId)) {
    validateNode(node);
    if (!entries.length && node.id !== rootId) throw new IntegrityError('subtree walk does not begin with its root');
    if (seen.has(node.id)) throw new IntegrityError('duplicate node in subtree walk');
    if (entries.length && (node.parent === null || !seen.has(node.parent))) throw new IntegrityError('subtree walk yielded a node before its parent');
    seen.add(node.id);
    const index = entries.length;
    const digest = sha256(SEAL_DOMAIN + canonicalText({ index, node_id: node.id, parent_id: node.parent ?? '', kind: node.kind, result_digest: node.resultDigest ?? '', previous }));
    entries.push({ index, node_id: node.id, parent_id: node.parent, kind: node.kind, result_digest: node.resultDigest, previous: previous || null, seal: digest });
    previous = digest;
  }
  if (!entries.length) throw new IntegrityError('subtree walk yielded no root');
  return deepFreeze({ root_id: rootId, algorithm: SEAL_ALGORITHM, digest: previous, entries });
}
/** Checks both the published digest and the complete ordered chain. */
export function verifySeal(store: Store, expected: SealReport): boolean {
  const actual = seal(store, expected.root_id);
  return resultToText(actual as unknown as import('./identity.js').JsonValue) === resultToText(expected as unknown as import('./identity.js').JsonValue);
}
export interface SealCustodyRecord { sequence: number; store_id: string; root_id: string; algorithm: string; digest: string; sealed_at: string; signer_identity: string; }
export class SQLiteSealSink {
  readonly path: string;
  readonly #db: SQLiteDatabase;
  constructor(path: string, options: { driver?: SQLiteDriver } = {}) {
    this.path=path; this.#db=openSQLite(path,false,options.driver);
    try {
      if (this.#db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'").get()) throw new TypeError('seal custody sink must not use a Pollard store database');
      this.#db.exec(`PRAGMA busy_timeout=30000; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS seal_custody_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS seal_custody_records(sequence INTEGER PRIMARY KEY AUTOINCREMENT,store_id TEXT NOT NULL,root_id TEXT NOT NULL,algorithm TEXT NOT NULL,digest TEXT NOT NULL,sealed_at TEXT NOT NULL,signer_identity TEXT NOT NULL);
        INSERT OR IGNORE INTO seal_custody_schema(singleton,version) VALUES(1,1);`);
      if(this.#db.prepare('SELECT version FROM seal_custody_schema WHERE singleton=1').get()?.version!==1) throw new IntegrityError('unsupported seal custody schema version');
    } catch(error) { this.#db.close(); throw error; }
  }
  close(): void { this.#db.close(); }
  publish(report: SealReport, options: { storeId: string; signerIdentity: string; sealedAt?: string }): SealCustodyRecord {
    for(const value of [options.storeId,options.signerIdentity]) if(typeof value!=='string' || !value) throw new TypeError('storeId and signerIdentity must be nonempty strings');
    if(report.algorithm!==SEAL_ALGORITHM || !/^[a-f0-9]{64}$/.test(report.root_id) || !/^[a-f0-9]{64}$/.test(report.digest)) throw new IntegrityError('invalid seal report');
    const timestamp=options.sealedAt??new Date().toISOString();
    if(typeof timestamp!=='string' || !timestamp) throw new TypeError('sealedAt must be a nonempty string');
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      const result=this.#db.prepare('INSERT INTO seal_custody_records(store_id,root_id,algorithm,digest,sealed_at,signer_identity) VALUES(?,?,?,?,?,?)').run(options.storeId,report.root_id,report.algorithm,report.digest,timestamp,options.signerIdentity);
      const sequence=Number(result.lastInsertRowid);
      if(!Number.isSafeInteger(sequence)) throw new IntegrityError('custody sequence exceeds the portable integer range');
      this.#db.exec('COMMIT');
      return {sequence,store_id:options.storeId,root_id:report.root_id,algorithm:report.algorithm,digest:report.digest,sealed_at:timestamp,signer_identity:options.signerIdentity};
    } catch(error) { this.#db.exec('ROLLBACK'); throw error; }
  }
  records(): SealCustodyRecord[] { return this.#db.prepare('SELECT * FROM seal_custody_records ORDER BY sequence').all().map(row=>({sequence:Number(row.sequence),store_id:String(row.store_id),root_id:String(row.root_id),algorithm:String(row.algorithm),digest:String(row.digest),sealed_at:String(row.sealed_at),signer_identity:String(row.signer_identity)})); }
}
