import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';

const require = createRequire(import.meta.url);
const source = readFileSync(new URL('../dist/esm/remote-kafka.cjs', import.meta.url), 'utf8');
const protocol = code => Object.assign(new Error('PRIVATE broker response with password=secret'), { name: 'KafkaJSProtocolError', code, retriable: ![29, 30, 31, 58].includes(code) });
const wrapped = cause => Object.assign(new Error('PRIVATE wrapper'), { name: 'KafkaJSNumberOfRetriesExceeded', cause, retriable: false });
const entry = () => Buffer.from(JSON.stringify({ format: 'pollardai/kafka/v1', store_id: 'test', operation_id: 'op', method: 'put', args: ['record'], at: 1 }));

function harness(behaviors, options = {}) {
  const clients = [], counts = { producerConnect: 0, sends: 0, adminClose: 0, applied: 0 };
  const admin = {
    async connect() {}, async disconnect() { counts.adminClose++; },
    async createTopics() {},
    async fetchTopicMetadata() { return { topics: [{ partitions: [{ partitionId: 0 }] }] }; },
    async describeConfigs() { return { resources: [{ configEntries: Object.entries({ 'retention.ms': '-1', 'retention.bytes': '-1', 'cleanup.policy': 'delete' }).map(([configName, configValue]) => ({ configName, configValue })) }] }; },
    async fetchTopicOffsets() { await options.onFetch?.(); return [{ partition: 0, low: options.low ?? '0', high: options.high ?? '0' }]; },
  };
  class Kafka {
    admin() { return admin; }
    producer() { return {
      async connect() { counts.producerConnect++; await options.onProducerConnect?.(); }, async disconnect() {},
      async send() { counts.sends++; throw options.sendError ?? protocol(7); },
    }; }
    consumer(config) {
      const listeners = new Map(), behavior = behaviors[Math.min(clients.length, behaviors.length - 1)];
      const client = {
        config, disconnected: 0, events: { GROUP_JOIN: 'join', CRASH: 'crash' },
        on(event, listener) { listeners.set(event, listener); },
        emit(event, payload) { listeners.get(event)?.({ payload }); },
        join(memberAssignment = { audit: [0] }) { this.emit('join', { memberAssignment }); },
        crash(error) { this.emit('crash', { error, restart: false }); },
        async message(value = entry(), offset = '0') { return this.eachMessage({ partition: 0, message: { offset, value } }); },
        async connect() {}, async subscribe() {},
        async disconnect() { this.disconnected++; await options.onConsumerDisconnect?.(); },
        async run({ autoCommit, eachMessage }) {
          assert.equal(autoCommit, false);
          this.eachMessage = eachMessage;
          try { await behavior(this); }
          catch (error) { this.crash(error); } // KafkaJS run() resolves after onCrash.
        },
      };
      clients.push(client);
      return client;
    }
  }
  const module = { exports: {} };
  runInNewContext(source, { module, exports: module.exports, require: name => {
    if (name === 'kafkajs') return { Kafka, ConfigResourceTypes: { TOPIC: 2 }, logLevel: { NOTHING: 0 } };
    if (name === 'node:perf_hooks' && options.freezeClock) return { performance: { now: () => 0 } };
    return require(name);
  }, setTimeout, clearTimeout });
  const operations = {
    createRemoteState: () => ({ records: [] }),
    isRemoteMutation: method => method === 'put',
    applyRemoteOperation(state, method, args) {
      if (method === 'put') { counts.applied++; return { state: { records: [...state.records, args[0]] }, result: true }; }
      return { state, result: state.records };
    },
  };
  return { clients, counts, open: () => module.exports.openKafkaBackend({ backend: 'kafka', brokers: ['unused:9092'], topic: 'audit', storeId: 'test', create: false, timeoutMs: options.timeoutMs ?? 1000 }, operations) };
}

test('failed Kafka group startup retries a fresh consumer before exposing an empty store', async () => {
  const h = harness([client => client.crash(wrapped(protocol(16))), client => client.join()]);
  const backend = await h.open();
  try {
    assert.equal(h.clients.length, 2);
    assert.equal(h.clients[0].disconnected, 1);
    assert.notEqual(h.clients[0].config.groupId, h.clients[1].config.groupId);
    const decision = h.clients[1].config.retry.restartOnFailure(protocol(16));
    assert.equal(typeof decision.then, 'function');
    assert.equal(await decision, false);
    assert.equal(h.counts.producerConnect, 0);
    h.clients[0].crash(protocol(30));
    await h.clients[0].message();
    assert.deepEqual(await backend.execute('roots', []), []);
    assert.equal(h.counts.applied, 0);
  } finally { await backend.close(); }
});

test('Kafka empty-topic startup waits for GROUP_JOIN after run resolves', async () => {
  const h = harness([() => {}]);
  let settled = false;
  const opening = h.open().then(backend => { settled = true; return backend; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false);
  h.clients[0].join();
  const backend = await opening;
  await backend.close();
});

test('Kafka startup without an assigned partition times out and closes its clients', async () => {
  const h = harness([() => {}], { timeoutMs: 25, freezeClock: true });
  await assert.rejects(h.open(), /startup timed out/);
  assert.equal(h.clients.length, 1);
  assert.equal(h.clients[0].disconnected, 1);
  assert.equal(h.counts.adminClose, 1);
});

test('Kafka startup rejects an invalid group assignment', async () => {
  const h = harness([client => client.join({ audit: [1] })]);
  await assert.rejects(h.open(), /did not acquire the audit partition/);
  assert.equal(h.clients.length, 1);
});

test('Kafka joined startup can recover a coordinator crash before reading any event', async () => {
  const h = harness([client => { client.join(); client.crash(protocol(16)); }, client => client.join()]);
  const backend = await h.open();
  assert.equal(h.clients.length, 2);
  assert.equal(h.counts.applied, 0);
  await backend.close();
});

test('Kafka timed-out run completion cannot revive a retired consumer', async () => {
  let finish;
  const h = harness([() => new Promise(resolve => { finish = resolve; })], { timeoutMs: 25, freezeClock: true });
  await assert.rejects(h.open(), /startup timed out/);
  assert.equal(h.clients[0].disconnected, 1);
  finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.clients[0].disconnected, 2);
  h.clients[0].join();
  await h.clients[0].message();
  assert.equal(h.counts.applied, 0);
});

test('Kafka coordinator discovery without a cause can retry only during startup', async () => {
  const missingCoordinator = Object.assign(new Error('PRIVATE coordinator'), { name: 'KafkaJSGroupCoordinatorNotFound', retriable: false });
  const h = harness([client => client.crash(missingCoordinator), client => client.join()]);
  const backend = await h.open();
  try {
    assert.equal(h.clients.length, 2);
    h.clients[1].crash(missingCoordinator);
    await assert.rejects(backend.execute('roots', []), /KafkaJSGroupCoordinatorNotFound/);
    await assert.rejects(h.clients[1].message());
    assert.equal(h.counts.applied, 0);
    assert.equal(h.clients.length, 2);
  } finally { await backend.close(); }
});

test('Kafka startup retries remain bounded by one deadline', async () => {
  const h = harness([client => client.crash(protocol(15))], { timeoutMs: 110 });
  await assert.rejects(h.open(), error => error.kafkaErrorCode === 15 || /startup timed out/.test(error.message));
  assert.ok(h.clients.length >= 1 && h.clients.length <= 3);
  assert.ok(h.clients.every(client => client.disconnected === 1));
  assert.equal(h.counts.producerConnect, 0);
});

for (const code of [2, 3, 29, 30, 31, 58]) {
  test(`Kafka protocol code ${code} fails closed without a startup retry or raw broker diagnostics`, async () => {
    const h = harness([client => client.crash(wrapped(protocol(code)))]);
    await assert.rejects(h.open(), error => {
      assert.equal(error.kafkaErrorCode, code);
      assert.equal(error.kafkaErrorName, 'KafkaJSProtocolError');
      assert.doesNotMatch(error.message, /PRIVATE|password|secret/);
      return true;
    });
    assert.equal(h.clients.length, 1);
  });
}

test('Kafka startup refuses to restart after the first replayed audit event', async () => {
  const h = harness([async client => { client.join(); await client.message(); client.crash(protocol(16)); }], { high: '1' });
  await assert.rejects(h.open(), error => error.kafkaErrorCode === 16);
  assert.equal(h.clients.length, 1);
  assert.equal(h.counts.applied, 1);
  await h.clients[0].message(); // Closed generations are ignored, including late callbacks.
  assert.equal(h.counts.applied, 1);
});

test('Kafka corrupt first event retains its integrity failure without retry', async () => {
  const h = harness([async client => { client.join(); await client.message(Buffer.from('{}')); }], { high: '1' });
  await assert.rejects(h.open(), /incompatible namespace or event/);
  assert.equal(h.clients.length, 1);
  assert.equal(h.counts.applied, 0);
});

test('Kafka truncation and regressed watermarks are terminal', async () => {
  for (const options of [{ low: '1' }, { high: '-1' }]) {
    const h = harness([client => client.join()], options);
    await assert.rejects(h.open(), /truncated|watermark moved behind/);
    assert.equal(h.clients.length, 1);
  }
});

test('Kafka uncertain sends poison the backend and are never retried', async () => {
  const h = harness([client => client.join()]);
  const backend = await h.open();
  try {
    await assert.rejects(backend.execute('put', ['new']), /append outcome is uncertain/);
    await assert.rejects(backend.execute('put', ['new']), /append outcome is uncertain/);
    assert.equal(h.counts.sends, 1);
    assert.equal(h.clients.length, 1);
  } finally { await backend.close(); }
});

test('Kafka concurrent replay advancing during watermark lookup is not false corruption', async () => {
  const options = {};
  const h = harness([client => client.join()], options);
  const backend = await h.open();
  try {
    options.onFetch = async () => { options.onFetch = undefined; await h.clients[0].message(); };
    assert.deepEqual(await backend.execute('roots', []), ['record']);
    assert.equal(h.counts.applied, 1);
  } finally { await backend.close(); }
});

test('Kafka confirmed watermark regression permanently poisons existing state', async () => {
  const options = { high: '1' };
  const h = harness([async client => { client.join(); await client.message(); }], options);
  const backend = await h.open();
  try {
    options.high = '0';
    await assert.rejects(backend.execute('roots', []), /watermark moved behind/);
    options.high = '1';
    await assert.rejects(backend.execute('roots', []), /watermark moved behind/);
  } finally { await backend.close(); }
});

test('Kafka crash during producer connection prevents any append', async () => {
  const options = {};
  const h = harness([client => client.join()], options);
  const backend = await h.open();
  try {
    options.onProducerConnect = () => h.clients[0].crash(protocol(16));
    await assert.rejects(backend.execute('put', ['new']), error => error.kafkaErrorCode === 16);
    assert.equal(h.counts.sends, 0);
  } finally { await backend.close(); }
});

test('Kafka failed startup cleanup retains safe diagnostics and never opens a replacement', async () => {
  for (const onConsumerDisconnect of [() => { throw new Error('PRIVATE cleanup password=secret'); }, () => new Promise(() => {})]) {
    const h = harness([client => client.crash(protocol(15))], { onConsumerDisconnect, timeoutMs: 50 });
    await assert.rejects(h.open(), error => {
      assert.doesNotMatch(error.message, /PRIVATE|password|secret/);
      return error.kafkaErrorCode === 15 || /startup timed out/.test(error.message);
    });
    assert.equal(h.clients.length, 1);
    assert.equal(h.counts.producerConnect, 0);
  }
});
