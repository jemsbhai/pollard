import test from 'node:test';
import assert from 'node:assert/strict';
import { HashRopeStore } from '../dist/esm/hashrope.js';
import { Node, IntegrityError, MissingNodeError } from '../dist/esm/tree.js';
import { Runtime } from '../dist/esm/runtime.js';

test('HashRope log bytes and polynomial hash match the Python 1.6 oracle', () => {
  const store = new HashRopeStore();
  const root = Node.make({ kind: 'root', parent: null, payload: { run: 'hashrope-oracle' } }); store.put(root);
  const node = Node.make({ kind: 'model_call', parent: root.id, payload: { model: 'local' }, result: { text: 'ok', usage: { input_tokens: 2, output_tokens: 1 } } }); store.put(node);
  store.updateMeta(node.id, { label: 'saved' });
  assert.equal(root.id, '60ab6cecbce0101e376e8b03d2f466e5c040680bd0881d7f4ff9ceb43c496438');
  assert.equal(node.id, '1f94c5ae96dbc2c632e5d7b53ac75f5c5a54008ecd15d22c9d768180e4b58535');
  // Generated with pollard 1.6.0 HashRopeStore + hashrope PolynomialHash defaults.
  assert.equal(store.contentHash(), 1574633237098298204n);
  store.validateLog();
  const restored = new HashRopeStore(store.toBytes());
  assert.deepEqual(restored.get(node.id).toStorage(), store.get(node.id).toStorage());
  assert.equal(restored.contentHash(), store.contentHash());
  const bytes = store.toBytes(); bytes.fill(0);
  store.validateLog();
});

test('HashRope retains original results and audits conflicting put operations', () => {
  const store = new HashRopeStore();
  const root = Node.make({ kind: 'root', parent: null, payload: { run: 'conflicts' } }); store.put(root);
  const first = Node.make({ kind: 'model_call', parent: root.id, payload: {}, result: { text: 'first' } }); store.put(first);
  store.put(Node.make({ kind: 'model_call', parent: root.id, payload: {}, result: { text: 'second' } }));
  store.updateMeta(first.id, { label: 'kept' });
  const restored = new HashRopeStore(store.toBytes());
  assert.equal(restored.get(first.id).result.text, 'first');
  assert.equal(restored.get(first.id).meta.result_conflicts[0].result.text, 'second');
  assert.equal(restored.get(first.id).meta.label, 'kept');
  restored.validateLog();
});

test('HashRope live recordings settle to interoperable put records and strictly replay', () => {
  const store = new HashRopeStore();
  const run = new Runtime({ store }).run('live');
  let interim;
  const node = run.modelCall({ model: 'local' }, () => { interim = new HashRopeStore(store.toBytes()); return { text: 'done', usage: { input_tokens: 1, output_tokens: 1 } }; });
  const pending = [...interim.walk(run.rootId)].find(item => item.kind === 'model_call');
  assert.equal(pending.meta.state, 'pending');
  const restored = new HashRopeStore(store.toBytes());
  assert.equal(restored.get(node.id).result.text, 'done');
  const replay = new Runtime({ store: restored, mode: 'replay' }).run('live');
  assert.equal(replay.modelCall({ model: 'local' }, () => { throw new Error('must not dispatch'); }).id, node.id);
  assert.equal(Buffer.from(store.toBytes()).toString().includes('"op":"finalize"'), false);
  store.validateLog();
});

test('HashRope rejects bad logs and missing parents before mutation', () => {
  for (const data of ['not json', '[]', '{"op":"other"}', '{"op":"meta","id":"bad","patch":1}', '{"op":"put"}']) assert.throws(() => new HashRopeStore(Buffer.from(data)), IntegrityError);
  assert.throws(() => new HashRopeStore(new Uint8Array([0xff])), IntegrityError);
  const store = new HashRopeStore();
  assert.throws(() => store.put(Node.make({ kind: 'note', parent: 'a'.repeat(64), payload: {} })), MissingNodeError);
  assert.equal(store.toBytes().length, 0);
});

test('HashRope transactions roll back both log and nodes and compaction preserves metadata', () => {
  const store = new HashRopeStore();
  const root = Node.make({ kind: 'root', parent: null, payload: { run: 'maintenance' } }); store.put(root);
  const before = store.toBytes();
  assert.throws(() => store.transaction(() => { store.updateMeta(root.id, { changed: true }); throw new Error('rollback'); }), /rollback/);
  assert.deepEqual(store.toBytes(), before);
  assert.equal(store.get(root.id).meta.changed, undefined);
  const child = Node.make({ kind: 'note', parent: root.id, payload: {} }); store.put(child);
  store.updateMeta(root.id, { tag: 'retained' });
  assert.throws(() => store.dropNodes(new Set([root.id])), IntegrityError);
  store.dropNodes(new Set([child.id]));
  assert.equal(store.compact(), 0);
  store.validateLog();
  assert.equal(store.get(root.id).meta.tag, 'retained');
  assert.deepEqual(new HashRopeStore(store.toBytes()).children(root.id), []);
});
