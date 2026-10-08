import { randomUUID } from 'node:crypto';
import type { RemoteBackend, RemoteBackendOptions, StateOperations } from './remote-backends.cjs';

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
  const kafka = new Kafka({ ...settings.clientConfig, brokers, clientId: settings.clientConfig?.clientId ?? 'pollardai', logLevel: logLevel.NOTHING });
  const admin = kafka.admin(), producer = kafka.producer({ allowAutoTopicCreation: false, idempotent: true, maxInFlightRequests: 1 });
  const consumer = kafka.consumer({ groupId: `pollardai-${randomUUID()}`, allowAutoTopicCreation: false, readUncommitted: false });
  let state = operations.createRemoteState(), nextOffset = 0n, poison: Error | undefined, producerConnected = false, closed = false;
  const pendingResults = new Map<string, { result?: unknown; error?: Error } | null>();
  const mutable = (method: string): boolean => operations.isRemoteMutation(method);
  async function close(): Promise<void> { if (closed) return; closed = true; await Promise.allSettled([consumer.disconnect(), producer.disconnect(), admin.disconnect()]); }
  async function waitOffset(target: bigint): Promise<void> {
    const deadline = Date.now() + timeout;
    while (nextOffset < target) {
      if (poison) throw poison;
      if (closed) throw new Error('KafkaStore is closed');
      if (Date.now() >= deadline) throw new Error('KafkaStore replay timed out; recording outcome may be uncertain');
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    if (poison) throw poison;
  }
  async function refresh(): Promise<void> {
    const offsets = await admin.fetchTopicOffsets(topic);
    if (offsets.length !== 1 || offsets[0].partition !== 0) throw new Error('KafkaStore requires exactly one topic partition');
    if (BigInt(offsets[0].low) !== 0n) throw new Error('KafkaStore audit history was truncated');
    await waitOffset(BigInt(offsets[0].high ?? offsets[0].offset));
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
    await consumer.connect(); await consumer.subscribe({ topic, fromBeginning: true });
    consumer.on(consumer.events.CRASH, (event: any) => { poison = new Error('KafkaStore consumer failed', { cause: event.payload.error }); });
    await consumer.run({ autoCommit: false, eachMessage: async ({ partition, message }: any) => {
      try {
        if (partition !== 0 || BigInt(message.offset) !== nextOffset) throw new Error('KafkaStore audit offsets are not contiguous');
        if (!message.value) throw new Error('KafkaStore audit log contains a tombstone');
        const entry = JSON.parse(message.value.toString('utf8'));
        if (entry.format !== 'pollardai/kafka/v1' || entry.store_id !== config.storeId || typeof entry.operation_id !== 'string' || typeof entry.method !== 'string' || !Array.isArray(entry.args) || !Number.isFinite(entry.at)) throw new Error('KafkaStore topic contains an incompatible namespace or event');
        if (!mutable(entry.method) || entry.method.startsWith('pollard') || ['dropNodes', 'compact', 'commitBatch'].includes(entry.method)) throw new Error('KafkaStore topic contains an unsupported operation');
        // Admission/finalization is decided at the log position, never from a
        // producer's earlier view. A racing command may be rejected normally.
        let outcome: { result?: unknown; error?: Error };
        try { const applied = operations.applyRemoteOperation(state, entry.method, entry.args, entry.at); state = applied.state; outcome = { result: applied.result }; }
        catch (error) { outcome = { error: error instanceof Error ? error : new Error('KafkaStore operation rejected') }; }
        if (pendingResults.has(entry.operation_id)) pendingResults.set(entry.operation_id, outcome);
        nextOffset++;
      } catch (error) { poison = error instanceof Error ? error : new Error('KafkaStore replay failed'); throw poison; }
    } });
    await refresh();
    return { close, async execute(method, args) {
      if (closed) throw new Error('KafkaStore is closed');
      if (poison) throw poison;
      if (method.startsWith('pollard') || ['transaction', 'begin', 'commit', 'rollback', 'dropNodes', 'compact', 'commitBatch', 'snapshot'].includes(method)) throw new TypeError('KafkaStore is an append-only audit store without shared arbitration or offline mutation');
      await refresh();
      const now = Date.now() / 1000, candidate = operations.applyRemoteOperation(state, method, args, now);
      if (!mutable(method)) return candidate.result;
      if (settings.readOnly) throw new TypeError('KafkaStore is read-only');
      if (!producerConnected) { await producer.connect(); producerConnected = true; }
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
