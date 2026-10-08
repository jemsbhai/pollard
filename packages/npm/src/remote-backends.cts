import { createHash } from 'node:crypto';

export interface RemoteBackendOptions {
  backend: 'postgres' | 'redis' | 'mongodb' | 'neo4j' | 'kafka';
  storeId: string;
  create: boolean;
  url?: string;
  database?: string;
  prefix?: string;
  username?: string;
  password?: string;
  timeoutMs?: number;
  topic?: string;
  brokers?: string[];
  [key: string]: unknown;
}
export interface StateOperations {
  createRemoteState(): unknown;
  applyRemoteOperation(state: any, method: string, args: unknown[], nowSeconds: number): { state: unknown; result: unknown };
  isRemoteMutation(method: string): boolean;
}
export interface RemoteBackend {
  execute(method: string, args: unknown[]): Promise<unknown>;
  close(): Promise<void>;
}
function missing(): never { throw Object.assign(new Error('remote Pollard namespace does not exist; pass create: true to initialize it'), { name: 'IntegrityError' }); }
function dependency(name: string): any {
  try { return require(name); }
  catch (cause) { throw new Error(`This Pollard backend requires the optional ${name} package; install it with npm install ${name}`, { cause }); }
}
function apply(ops: StateOperations, text: string | null | undefined, method: string, args: unknown[], now: number): { text: string; result: unknown } {
  if (text === null || text === undefined) missing();
  if (typeof text !== 'string') throw Object.assign(new Error('remote Pollard namespace has invalid serialized state'), { name: 'IntegrityError' });
  let state: unknown;
  try { state = JSON.parse(text); } catch { throw Object.assign(new Error('remote Pollard namespace has invalid JSON state'), { name: 'IntegrityError' }); }
  const applied = ops.applyRemoteOperation(state, method, args, now);
  return { text: JSON.stringify(applied.state), result: applied.result };
}
const delay = (ms: number): Promise<void> => new Promise(resolve => setTimeout(resolve, ms));

async function postgres(config: RemoteBackendOptions, ops: StateOperations): Promise<RemoteBackend> {
  const { Client } = dependency('pg');
  const client = new Client({ connectionString: config.url, connectionTimeoutMillis: config.timeoutMs, query_timeout: config.timeoutMs });
  // Keep idle socket errors within the RPC boundary; later queries reject with
  // the connection failure instead of terminating the worker unexpectedly.
  client.on('error', () => undefined);
  try {
    await client.connect();
    if (config.create) {
      await client.query('CREATE TABLE IF NOT EXISTS pollard_npm_state (store_id TEXT PRIMARY KEY, state TEXT NOT NULL)');
      await client.query('INSERT INTO pollard_npm_state (store_id,state) VALUES ($1,$2) ON CONFLICT (store_id) DO NOTHING', [config.storeId, JSON.stringify(ops.createRemoteState())]);
    }
  } catch (error) { await client.end().catch(() => undefined); throw error; }
  return {
    async execute(method, args) {
      const mutation = ops.isRemoteMutation(method);
      for (let attempt = 0; ; attempt++) {
        try {
          await client.query('BEGIN ISOLATION LEVEL SERIALIZABLE');
          const rows = await client.query(`SELECT state FROM pollard_npm_state WHERE store_id=$1${mutation ? ' FOR UPDATE' : ''}`, [config.storeId]);
          if (rows.rowCount !== 1) missing();
          const clock = await client.query('SELECT EXTRACT(EPOCH FROM clock_timestamp())::float8 AS now');
          const applied = apply(ops, rows.rows[0].state, method, args, Number(clock.rows[0].now));
          if (mutation) await client.query('UPDATE pollard_npm_state SET state=$2 WHERE store_id=$1', [config.storeId, applied.text]);
          await client.query('COMMIT');
          return applied.result;
        } catch (error) {
          await client.query('ROLLBACK').catch(() => undefined);
          if (attempt < 15 && ['40001', '40P01'].includes((error as { code?: string }).code ?? '')) { await delay(Math.min(5 * (attempt + 1), 80)); continue; }
          throw error;
        }
      }
    },
    async close() { await client.end(); },
  };
}

async function redis(config: RemoteBackendOptions, ops: StateOperations): Promise<RemoteBackend> {
  const redis = dependency('redis');
  const client = redis.createClient({ url: config.url, socket: { connectTimeout: config.timeoutMs, reconnectStrategy: false } });
  // Clients emit connection failures as events in addition to rejecting commands.
  client.on('error', () => undefined);
  const tag = createHash('sha256').update(config.storeId).digest('hex');
  const key = `{pollard-npm-${tag}}:${config.prefix ?? 'pollard'}:state`;
  try {
    await client.connect();
    if (config.create) await client.set(key, JSON.stringify(ops.createRemoteState()), { NX: true });
  } catch (error) { client.destroy(); throw error; }
  return {
    async execute(method, args) {
      const mutation = ops.isRemoteMutation(method);
      for (let attempt = 0; ; attempt++) {
        try {
          if (mutation) await client.watch(key);
          const value = await client.get(key);
          const time = await client.sendCommand(['TIME']);
          const applied = apply(ops, value, method, args, Number(time[0]) + Number(time[1]) / 1e6);
          if (mutation) {
            const result = await client.multi().set(key, applied.text).exec();
            if (result === null) throw new redis.WatchError();
          }
          return applied.result;
        } catch (error) {
          if (mutation) await client.unwatch().catch(() => undefined);
          if (attempt < 63 && (error instanceof redis.WatchError || (error as Error).name === 'WatchError')) { await delay(Math.min(attempt + 1, 30)); continue; }
          throw error;
        }
      }
    },
    async close() { if (client.isOpen) await client.quit(); },
  };
}

async function mongodb(config: RemoteBackendOptions, ops: StateOperations): Promise<RemoteBackend> {
  const { MongoClient } = dependency('mongodb');
  const client = new MongoClient(config.url, { serverSelectionTimeoutMS: config.timeoutMs, connectTimeoutMS: config.timeoutMs });
  const database = config.database ?? 'pollard';
  const prefix = config.prefix ?? 'pollard_npm';
  if (!/^[a-zA-Z0-9_-]+$/.test(prefix)) throw new TypeError('MongoDB prefix must contain only letters, digits, underscores and hyphens');
  const collection = client.db(database).collection(`${prefix}_state`);
  try {
    await client.connect();
    const hello = await client.db(database).command({ hello: 1 });
    if (!hello.setName && hello.msg !== 'isdbgrid') throw new Error('MongoStore requires a replica set or sharded deployment for transactions');
    if (config.create) await collection.updateOne({ _id: config.storeId }, { $setOnInsert: { state: JSON.stringify(ops.createRemoteState()) } }, { upsert: true });
  } catch (error) { await client.close(); throw error; }
  return {
    async execute(method, args) {
      const session = client.startSession();
      try {
        let result: unknown;
        await session.withTransaction(async () => {
          const row = await collection.aggregate([{ $match: { _id: config.storeId } }, { $project: { state: 1, now: '$$NOW' } }], { session }).next();
          if (!row) missing();
          const applied = apply(ops, row.state, method, args, row.now.getTime() / 1000);
          if (ops.isRemoteMutation(method)) await collection.updateOne({ _id: config.storeId }, { $set: { state: applied.text } }, { session });
          result = applied.result;
        }, { readConcern: { level: 'snapshot' }, writeConcern: { w: 'majority' }, readPreference: 'primary', maxCommitTimeMS: config.timeoutMs });
        return result;
      } finally { await session.endSession(); }
    },
    async close() { await client.close(); },
  };
}

async function neo4j(config: RemoteBackendOptions, ops: StateOperations): Promise<RemoteBackend> {
  const neo4j = dependency('neo4j-driver');
  const driver = neo4j.driver(config.url, neo4j.auth.basic(config.username, config.password), { connectionTimeout: config.timeoutMs, maxTransactionRetryTime: config.timeoutMs });
  const sessionOptions = { database: config.database ?? 'neo4j', defaultAccessMode: neo4j.session.WRITE };
  try {
    await driver.verifyConnectivity();
    if (config.create) {
      const session = driver.session(sessionOptions);
      try {
        await session.run('CREATE CONSTRAINT pollard_npm_store_id IF NOT EXISTS FOR (s:PollardNpmStore) REQUIRE s.id IS UNIQUE');
        await session.executeWrite((tx: any) => tx.run('MERGE (s:PollardNpmStore {id:$id}) ON CREATE SET s.state=$state, s.revision=0 RETURN s.id', { id: config.storeId, state: JSON.stringify(ops.createRemoteState()) }));
      } finally { await session.close(); }
    }
  } catch (error) { await driver.close(); throw error; }
  return {
    async execute(method, args) {
      const session = driver.session(sessionOptions);
      const mutation = ops.isRemoteMutation(method);
      try {
        // Route reads to the primary as well: different store handles do not
        // share driver bookmarks and must observe freshly committed claims.
        return await session.executeWrite(async (tx: any) => {
          const rows = await tx.run(`MATCH (s:PollardNpmStore {id:$id}) ${mutation ? 'SET s.revision=coalesce(s.revision,0)+1 ' : ''}RETURN s.state AS state`, { id: config.storeId });
          if (rows.records.length !== 1) missing();
          const clock = await tx.run('RETURN datetime.realtime().epochMillis / 1000.0 AS now');
          const applied = apply(ops, rows.records[0].get('state'), method, args, Number(clock.records[0].get('now')));
          if (mutation) await tx.run('MATCH (s:PollardNpmStore {id:$id}) SET s.state=$state', { id: config.storeId, state: applied.text });
          return applied.result;
        });
      } finally { await session.close(); }
    },
    async close() { await driver.close(); },
  };
}

export async function openRemoteBackend(config: RemoteBackendOptions, ops: StateOperations): Promise<RemoteBackend> {
  if (config.backend === 'postgres') return postgres(config, ops);
  if (config.backend === 'redis') return redis(config, ops);
  if (config.backend === 'mongodb') return mongodb(config, ops);
  if (config.backend === 'neo4j') return neo4j(config, ops);
  if (config.backend === 'kafka') return (await import('./remote-kafka.cjs')).openKafkaBackend(config, ops);
  throw new TypeError('unsupported remote backend');
}
