import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import type { RemoteBackend, RemoteBackendOptions, StateOperations } from './remote-backends.cjs';

// KafkaJS can wrap protocol errors more than once when its request retries run
// out. Do not treat retriable=true alone as safe: corrupt messages have it too.
function kafkaCause(error: any): any {
  const seen = new Set<unknown>();
  while (error && !seen.has(error) && seen.size < 8) {
    seen.add(error);
    if (!['KafkaJSNumberOfRetriesExceeded', 'KafkaJSNonRetriableError', 'KafkaStoreConsumerError'].includes(error.name) || !error.cause) return error;
    error = error.cause;
  }
  return undefined;
}
function retryStartup(error: unknown): boolean {
  const cause = kafkaCause(error);
  if (!cause) return false;
  // KafkaJS loses the underlying discovery error in brokerPool.withBroker.
  // Retry this exact class only before exposing a consumer or reading an event.
  if (cause.name === 'KafkaJSGroupCoordinatorNotFound') return true;
  if (cause.retriable !== true) return false;
  if (cause.name === 'KafkaJSProtocolError') return [5, 6, 7, 9, 13, 14, 15, 16, 74, 75].includes(cause.code);
  if (cause.name === 'KafkaJSConnectionClosedError' || cause.name === 'KafkaJSRequestTimeoutError') return true;
  return cause.name === 'KafkaJSConnectionError' && ['ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'EPIPE', 'EAI_AGAIN'].includes(cause.code);
}
function consumerFailure(cause: unknown): Error {
  const original = kafkaCause(cause);
  const names = ['KafkaJSProtocolError', 'KafkaJSConnectionError', 'KafkaJSConnectionClosedError', 'KafkaJSRequestTimeoutError', 'KafkaJSGroupCoordinatorNotFound', 'KafkaJSSASLAuthenticationError', 'KafkaJSNonRetriableError', 'KafkaJSNumberOfRetriesExceeded'];
  const kafkaErrorName = names.includes(original?.name) ? original.name : 'Error';
  const kafkaErrorCode = Number.isSafeInteger(original?.code) ? original.code : undefined;
  // Raw broker messages can contain credentials or application data. Only
  // fixed driver names and numeric protocol codes cross the worker boundary.
  return Object.assign(new Error(`KafkaStore consumer failed (${kafkaErrorName}${kafkaErrorCode === undefined ? '' : `, code ${kafkaErrorCode}`})`, { cause }), {
    name: 'KafkaStoreConsumerError', kafkaErrorName, ...(kafkaErrorCode === undefined ? {} : { kafkaErrorCode }),
  });
}

/** One dedicated, ordered audit topic. Kafka deliberately supplies no budget arbiter. */
export async function openKafkaBackend(config: RemoteBackendOptions, operations: StateOperations): Promise<RemoteBackend> {
  const settings = config as unknown as Record<string, any>;
  const { Kafka, ConfigResourceTypes, logLevel } = require('kafkajs');
  const brokers = settings.brokers;
  if (!Array.isArray(brokers) || !brokers.length || brokers.some((value: unknown) => typeof value !== 'string' || !value)) throw new TypeError('KafkaStore requires a nonempty brokers array');
  const topic = settings.topic;
  if (typeof topic !== 'string' || !/^[a-zA-Z0-9._-]+$/.test(topic) || topic === '.' || topic === '..') throw new TypeError('KafkaStore requires a valid dedicated topic');
  const timeout = typeof settings.timeoutMs === 'number' ? settings.timeoutMs : 30_000;
  if (settings.readOnly !== undefined && typeof settings.readOnly !== 'boolean') throw new TypeError('Kafka readOnly must be boolean');
  if (settings.readOnly && config.create) throw new TypeError('read-only KafkaStore cannot create a topic');
  const startupDeadline = performance.now() + timeout;
  const kafka = new Kafka({ ...settings.clientConfig, brokers, clientId: settings.clientConfig?.clientId ?? 'pollardai', logLevel: logLevel.NOTHING });
  const admin = kafka.admin(), producer = kafka.producer({ allowAutoTopicCreation: false, idempotent: true, maxInFlightRequests: 1 });
  let consumer: any;
  let state = operations.createRemoteState(), nextOffset = 0n, poison: Error | undefined, producerConnected = false, closed = false, replayStarted = false;
  const pendingResults = new Map<string, { result?: unknown; error?: Error } | null>();
  const mutable = (method: string): boolean => operations.isRemoteMutation(method);
  async function close(): Promise<void> { if (closed) return; closed = true; await Promise.allSettled([consumer?.disconnect(), producer.disconnect(), admin.disconnect()]); }
  async function startup<T>(action: () => Promise<T>, lateCleanup?: () => Promise<void>): Promise<T> {
    const remaining = startupDeadline - performance.now();
    if (remaining <= 0) throw new Error('KafkaStore consumer startup timed out');
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    const pending = action();
    // KafkaJS has no AbortSignal for connect/run. Fence the attempt immediately
    // and disconnect again if an in-flight startup operation completes late.
    if (lateCleanup) void pending.then(async () => { if (expired) await lateCleanup(); }, async () => { if (expired) await lateCleanup(); }).catch(() => undefined);
    try {
      return await Promise.race([pending, new Promise<never>((_, reject) => {
        timer = setTimeout(() => { expired = true; reject(new Error('KafkaStore consumer startup timed out')); }, remaining);
      })]);
    } finally { if (timer !== undefined) clearTimeout(timer); }
  }
  async function retire(attempt: any): Promise<boolean> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        Promise.resolve().then(() => attempt.disconnect()).then(() => true, () => false),
        new Promise<false>(resolve => { timer = setTimeout(() => resolve(false), Math.max(0, startupDeadline - performance.now())); }),
      ]);
    } finally { if (timer !== undefined) clearTimeout(timer); }
  }
  async function waitOffset(target: bigint, deadline = performance.now() + timeout): Promise<void> {
    while (nextOffset < target) {
      if (poison) throw poison;
      if (closed) throw new Error('KafkaStore is closed');
      if (performance.now() >= deadline) throw new Error('KafkaStore replay timed out; recording outcome may be uncertain');
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    if (poison) throw poison;
  }
  async function refresh(deadline?: number): Promise<void> {
    const minimumHigh = nextOffset;
    const offsets = await admin.fetchTopicOffsets(topic);
    if (offsets.length !== 1 || offsets[0].partition !== 0) throw poison ??= new Error('KafkaStore requires exactly one topic partition');
    if (BigInt(offsets[0].low) !== 0n) throw poison ??= new Error('KafkaStore audit history was truncated');
    const high = BigInt(offsets[0].high ?? offsets[0].offset);
    if (high < minimumHigh) throw poison ??= new Error('KafkaStore high watermark moved behind replay cursor');
    await waitOffset(high, deadline);
  }
  try {
    await admin.connect();
    if (config.create) await admin.createTopics({ waitForLeaders: true, topics: [{ topic, numPartitions: 1, replicationFactor: settings.replicationFactor ?? 1, configEntries: [
      { name: 'cleanup.policy', value: 'delete' }, { name: 'retention.ms', value: '-1' }, { name: 'retention.bytes', value: '-1' },
    ] }] });
    const metadata = await admin.fetchTopicMetadata({ topics: [topic] });
    if (metadata.topics.length !== 1 || metadata.topics[0].partitions.length !== 1) throw new Error('KafkaStore requires exactly one topic partition');
    const description = await admin.describeConfigs({ resources: [{ type: ConfigResourceTypes.TOPIC, name: topic }] });
    const entries = Object.fromEntries(description.resources[0].configEntries.map((entry: any) => [entry.configName, entry.configValue]));
    if (entries['retention.ms'] !== '-1' || entries['retention.bytes'] !== '-1' || entries['cleanup.policy'] !== 'delete') throw new Error('KafkaStore requires infinite retention and deletion-only cleanup (no compaction)');
    let delay = 50;
    for (;;) {
      const attempt = kafka.consumer({ groupId: `pollardai-${randomUUID()}`, allowAutoTopicCreation: false, readUncommitted: false,
        retry: { restartOnFailure: async () => false } });
      consumer = attempt;
      let joined = false;
      attempt.on(attempt.events.GROUP_JOIN, (event: any) => {
        if (closed || consumer !== attempt) return;
        const assigned = event.payload.memberAssignment?.[topic];
        if (!Array.isArray(assigned) || assigned.length !== 1 || assigned[0] !== 0) poison ??= new Error('KafkaStore consumer did not acquire the audit partition');
        else joined = true;
      });
      attempt.on(attempt.events.CRASH, (event: any) => { if (!closed && consumer === attempt) poison ??= consumerFailure(event.payload.error); });
      try {
        await startup(() => attempt.connect().catch((cause: unknown) => { throw consumerFailure(cause); }), () => attempt.disconnect());
        await startup(() => attempt.subscribe({ topic, fromBeginning: true }).catch((cause: unknown) => { throw consumerFailure(cause); }), () => attempt.disconnect());
        await startup(() => attempt.run({ autoCommit: false, eachMessage: async ({ partition, message }: any) => {
          if (closed || consumer !== attempt) return;
          if (poison) throw poison;
          replayStarted = true;
          try {
            if (partition !== 0 || BigInt(message.offset) !== nextOffset) throw new Error('KafkaStore audit offsets are not contiguous');
            if (!message.value) throw new Error('KafkaStore audit log contains a tombstone');
            const entry = JSON.parse(message.value.toString('utf8'));
            if (entry.format !== 'pollardai/kafka/v1' || entry.store_id !== config.storeId || typeof entry.operation_id !== 'string' || typeof entry.method !== 'string' || !Array.isArray(entry.args) || !Number.isFinite(entry.at)) throw new Error('KafkaStore topic contains an incompatible namespace or event');
            if (!mutable(entry.method) || entry.method.startsWith('pollard') || ['dropNodes', 'compact', 'commitBatch'].includes(entry.method)) throw new Error('KafkaStore topic contains an unsupported operation');
            // Admission/finalization is decided at the log position, never from
            // a producer's earlier view. A racing command may be rejected normally.
            let outcome: { result?: unknown; error?: Error };
            try { const applied = operations.applyRemoteOperation(state, entry.method, entry.args, entry.at); state = applied.state; outcome = { result: applied.result }; }
            catch (error) { outcome = { error: error instanceof Error ? error : new Error('KafkaStore operation rejected') }; }
            if (pendingResults.has(entry.operation_id)) pendingResults.set(entry.operation_id, outcome);
            nextOffset++;
          } catch (error) { poison = error instanceof Error ? error : new Error('KafkaStore replay failed'); throw poison; }
        } }).catch((cause: unknown) => { throw poison ?? consumerFailure(cause); }), () => attempt.disconnect());
        // run() can resolve after KafkaJS catches a join failure and emits
        // CRASH. Even an empty topic must have a confirmed partition owner.
        await startup(async () => {
          while (!joined) {
            if (closed || consumer !== attempt) throw new Error('KafkaStore consumer startup was closed');
            if (poison) throw poison;
            await new Promise(resolve => setTimeout(resolve, 5));
          }
          if (poison) throw poison;
          await refresh(startupDeadline);
        });
        break;
      } catch (error) {
        const failure = poison ?? error;
        consumer = undefined; // Fence callbacks from the discarded generation.
        // Failed cleanup must neither leak a broker's raw error nor permit a
        // replacement consumer while the previous generation may still run.
        if (!await retire(attempt)) throw failure;
        if (replayStarted || !retryStartup(failure) || performance.now() >= startupDeadline) throw failure;
        await new Promise(resolve => setTimeout(resolve, Math.min(delay, Math.max(0, startupDeadline - performance.now()))));
        delay = Math.min(delay * 2, 250);
        if (performance.now() >= startupDeadline) throw failure;
        poison = undefined;
      }
    }
    return { close, async execute(method, args) {
      if (closed) throw new Error('KafkaStore is closed');
      if (poison) throw poison;
      if (method.startsWith('pollard') || ['transaction', 'begin', 'commit', 'rollback', 'dropNodes', 'compact', 'commitBatch', 'snapshot'].includes(method)) throw new TypeError('KafkaStore is an append-only audit store without shared arbitration or offline mutation');
      await refresh();
      const now = Date.now() / 1000, candidate = operations.applyRemoteOperation(state, method, args, now);
      if (!mutable(method)) return candidate.result;
      if (settings.readOnly) throw new TypeError('KafkaStore is read-only');
      if (!producerConnected) { await producer.connect(); producerConnected = true; }
      if (closed) throw new Error('KafkaStore is closed');
      if (poison) throw poison;
      const operationId = randomUUID(); pendingResults.set(operationId, null);
      let outcome: { result?: unknown; error?: Error } | null | undefined;
      try {
        const records = await producer.send({ topic, acks: -1, messages: [{ partition: 0, key: config.storeId, value: JSON.stringify({ format: 'pollardai/kafka/v1', store_id: config.storeId, operation_id: operationId, method, args, at: now }) }] });
        if (records.length !== 1 || records[0].baseOffset === undefined) throw new Error('KafkaStore acknowledgement omitted the audit offset');
        await waitOffset(BigInt(records[0].baseOffset) + 1n);
        outcome = pendingResults.get(operationId);
        if (!outcome) throw new Error('KafkaStore replay did not acknowledge this operation');
      } catch (cause) { poison = new Error('KafkaStore append outcome is uncertain; reopen the store to reconcile before retrying', { cause }); throw poison; }
      finally { pendingResults.delete(operationId); }
      if (outcome.error) throw outcome.error;
      return outcome.result;
    } };
  } catch (error) { await close(); throw error; }
}
