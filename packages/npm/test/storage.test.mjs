import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import Database from 'better-sqlite3';
import { Node, MemoryStore, IntegrityError } from '../dist/esm/tree.js';
import { resultDigestFromText } from '../dist/esm/identity.js';
import { SQLiteStore } from '../dist/esm/stores.js';
import { seal, verifySeal, SQLiteSealSink } from '../dist/esm/seal.js';
import { exportSubtree, importSubtree, exportJSONL, importJSONL, merge, gc } from '../dist/esm/governance.js';

const temporary = t => { const path=mkdtempSync(join(tmpdir(),'pollard-storage-')); t.after(()=>rmSync(path,{recursive:true,force:true})); return path; };
const tree = store => {
  const root=Node.make({kind:'root',parent:null,payload:{run:'storage'}});
  const child=Node.make({kind:'model_call',parent:root.id,payload:{model:'mock',prompt:'x'.repeat(2048)},result:{answer:'yes'}});
  const branch=Node.make({kind:'note',parent:root.id,payload:{label:'pruned',prompt:'y'.repeat(2048)},meta:{pruned:true}});
  const descendant=Node.make({kind:'note',parent:branch.id,payload:{label:'child'}});
  for(const node of [root,child,branch,descendant]) store.put(node);
  return {root,child,branch,descendant};
};

test('SQLite preserves exact result text, interning, literal refs, read-only replay and append-only conflict evidence',t=>{
  const dir=temporary(t),path=join(dir,'store.db'),store=new SQLiteStore(path,{internThreshold:16});
  const root=Node.make({kind:'root',parent:null,payload:{run:'precise'}}); store.put(root);
  const literal={__pollard_ref:'a'.repeat(64)};
  const base=Node.make({kind:'model_call',parent:root.id,payload:{prompt:'π'.repeat(32),literal,nested:[literal]}});
  const exact='{ "value": 1.00, "unicode": "\\u03c0" }';
  const node=Node.fromStorage({...base.toStorage(),result_text:exact,result_digest:resultDigestFromText(exact)}); store.put(node);
  store.put(Node.make({kind:node.kind,parent:node.parent,payload:node.payload,result:{value:2}}));
  assert.equal(store.get(node.id).resultText,exact);
  assert.deepEqual(store.get(node.id).payload,node.payload);
  assert.equal(store.get(node.id).meta.result_conflicts.length,1);
  store.close();
  const db=new Database(path); assert.equal(db.prepare('SELECT count(*) AS n FROM blobs').get().n,1); db.close();
  const replay=new SQLiteStore(path,{readOnly:true});
  assert.equal(replay.get(node.id).resultText,exact);
  assert.throws(()=>replay.updateMeta(node.id,{x:true}),/readonly|read-only/i);
  replay.close();
});

test('SQLite detects tampered blobs and refuses unsupported schema without downgrading it',t=>{
  const dir=temporary(t),path=join(dir,'store.db'),store=new SQLiteStore(path); const {child}=tree(store); store.close();
  const db=new Database(path); db.prepare('UPDATE blobs SET value=?').run('corrupt'); db.close();
  const corrupt=new SQLiteStore(path); assert.throws(()=>corrupt.get(child.id),IntegrityError); corrupt.close();
  const edit=new Database(path); edit.prepare("UPDATE kv SET v='99' WHERE k='schema_version'").run(); edit.close();
  assert.throws(()=>new SQLiteStore(path),/unsupported SQLite schema/);
  const check=new Database(path); assert.equal(check.prepare("SELECT v FROM kv WHERE k='schema_version'").get().v,'99'); check.close();
});

test('pending completion is single-use, cannot be adopted by another SQLite handle, and rolls back',t=>{
  const path=join(temporary(t),'store.db'),first=new SQLiteStore(path),second=new SQLiteStore(path);
  const root=Node.make({kind:'root',parent:null,payload:{run:'pending'}}); first.put(root);
  const pending=Node.make({kind:'tool_call',parent:root.id,payload:{tool:'send'},meta:{state:'pending'}}); first.put(pending);
  const done=Node.make({kind:pending.kind,parent:pending.parent,payload:pending.payload,result:{sent:true},meta:{state:'completed'}});
  assert.throws(()=>second.finalize(done),/pending dispatch/);
  assert.throws(()=>first.transaction(()=>{first.finalize(done);throw Error('rollback');}),/rollback/);
  assert.equal(first.get(pending.id).meta.state,'pending'); first.finalize(done);
  assert.throws(()=>first.finalize(done),/pending dispatch/);
  first.close(); second.close();
});

for(const backend of ['memory','sqlite']) test(`${backend} sealed export/import is interoperable, idempotent and rejects tampering before mutation`,t=>{
  const dir=temporary(t),source=backend==='memory'?new MemoryStore():new SQLiteStore(join(dir,'src.db')),target=backend==='memory'?new MemoryStore():new SQLiteStore(join(dir,'dst.db'));
  const {root,child}=tree(source),path=join(dir,'tree.json');
  const before=seal(source,root.id),exported=exportSubtree(source,root.id,path),imported=importSubtree(path,target);
  assert.equal(imported.imported,4); assert.equal(imported.digest,exported.digest); assert.ok(verifySeal(target,before));
  assert.equal(importSubtree(path,target).existing,4);
  const jsonl=join(dir,'tree.jsonl'); exportJSONL(source,root.id,jsonl); assert.equal(importJSONL(jsonl,new MemoryStore()).imported,4);
  const document=JSON.parse(readFileSync(path,'utf8')); document.nodes.find(node=>node.id===child.id).result='{"bad":true}'; writeFileSync(path,JSON.stringify(document));
  const empty=new MemoryStore(); assert.throws(()=>importSubtree(path,empty),IntegrityError); assert.deepEqual(empty.roots(),[]);
  source.close?.(); target.close?.();
});

test('detached subtree requires parent and strict deterministic manifest order',t=>{
  const dir=temporary(t),source=new MemoryStore(),{root,branch}=tree(source),path=join(dir,'detached.json');
  exportSubtree(source,branch.id,path); const target=new MemoryStore(); assert.throws(()=>importSubtree(path,target),/parent is missing/); target.put(root); assert.equal(importSubtree(path,target).imported,2);
  exportSubtree(source,root.id,path); const doc=JSON.parse(readFileSync(path,'utf8')); [doc.nodes[1],doc.nodes[2]]=[doc.nodes[2],doc.nodes[1]]; writeFileSync(path,JSON.stringify(doc)); assert.throws(()=>importSubtree(path,new MemoryStore()),IntegrityError);
});

for(const backend of ['memory','sqlite']) test(`${backend} merge preserves destination results and metadata conflicts, and is atomic on backend failure`,t=>{
  const dir=temporary(t),target=backend==='memory'?new MemoryStore():new SQLiteStore(join(dir,'target.db')),source=new MemoryStore();
  const root=Node.make({kind:'root',parent:null,payload:{run:'merge'},meta:{nested:{label:'old',list:[1]}}}); target.put(root);
  source.put(Node.make({kind:'root',parent:null,payload:root.payload,meta:{nested:{label:'new',list:[2]},added:true}}));
  const child=Node.make({kind:'model_call',parent:root.id,payload:{x:1},result:{old:true}}); target.put(child); source.put(Node.make({kind:child.kind,parent:child.parent,payload:child.payload,result:{new:true}}));
  const report=merge(target,source,{requireAtomic:true}); assert.equal(report.resultConflicts,1); assert.equal(report.metaConflicts,1);
  assert.deepEqual(target.get(child.id).result,{old:true}); assert.deepEqual(target.get(root.id).meta.nested.list,[1,2]); assert.equal(target.get(root.id).meta.merge_conflicts[0].path,'nested.label');
  assert.equal(merge(target,source).resultConflicts,0); assert.equal(merge(target,source).metaConflicts,0);
  const prior=target.get(root.id).toStorage(); assert.throws(()=>merge(target,source,{replay:true}),/result collision/); assert.deepEqual(target.get(root.id).toStorage(),prior);
  const extra=new MemoryStore(),other=Node.make({kind:'root',parent:null,payload:{run:'other'}}); extra.put(other); extra.put(Node.make({kind:'note',parent:other.id,payload:{fail:true}}));
  const original=target.put.bind(target); target.put=node=>{ if(node.kind==='note') throw Error('backend failure'); original(node); };
  assert.throws(()=>merge(target,extra),/backend failure/); assert.equal(target.exists(other.id),false);
  target.close?.();
});

for(const backend of ['memory','sqlite']) test(`${backend} offline GC retains siblings, removes descendants, and compacts only unused blobs`,t=>{
  const store=backend==='memory'?new MemoryStore():new SQLiteStore(join(temporary(t),'gc.db')),{root,child,branch,descendant}=tree(store);
  const report=gc(store); assert.deepEqual(new Set(report.removedNodes),new Set([branch.id,descendant.id])); assert.ok(store.exists(child.id)); assert.equal(report.survivorSeals[root.id],seal(store,root.id).digest);
  assert.equal(gc(store,{mode:'compact'}).removedBlobs,backend==='sqlite'?1:0); store.close?.();
});

test('SQLite reservation admission, exact decimal settlement, renewal and windows are shared across handles',t=>{
  const path=join(temporary(t),'budget.db'),first=new SQLiteStore(path),second=new SQLiteStore(path);
  const request=amount=>[{scopeId:'shared',limits:{usd:0.3},baseline:{usd:0},estimates:{usd:amount}}];
  assert.equal(first.pollardReserve('one',request(0.1),[],30).ok,true);
  assert.equal(second.pollardReserve('two',request(0.2),[],30).ok,true);
  assert.equal(second.pollardReserve('three',request(0.01),[],30).ok,false);
  first.pollardSettle('one',{usd:0.1}); second.pollardSettle('two',{usd:0.2}); assert.equal(first.pollardReserve('zero',request(0),[],30).ok,true);
  assert.equal(second.pollardRenew('zero',30),true); second.pollardRelease('zero'); assert.equal(first.pollardRenew('zero',30),false);
  const window=[{ledgerKey:'api',meter:'calls',limit:1,amount:1,windowSeconds:60}];
  assert.equal(first.pollardReserve('w1',[],window,30).ok,true); assert.equal(second.pollardReserve('w2',[],window,30).reason,'window');
  first.pollardRelease('w1'); assert.equal(second.pollardReserve('w2',[],window,30).ok,true); second.pollardSettle('w2',{calls:1}); assert.equal(first.pollardReserve('w3',[],window,30).ok,false);
  first.close(); second.close();
});

test('in-memory SQLite reservations do not expire during synchronous work',()=>{
  const store=new SQLiteStore(':memory:');
  try {
    assert.equal(store.pollardReserve('in-process',[{scopeId:'run',limits:{steps:1},baseline:{},estimates:{steps:1}}],[],0.001).ok,true);
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,5);
    assert.equal(store.pollardRenew('in-process',0.001),true);
    store.pollardSettle('in-process',{steps:1});
    assert.equal(store.pollardReserve('later',[{scopeId:'run',limits:{steps:1},baseline:{},estimates:{steps:1}}],[],1).ok,false);
  } finally {store.close();}
});

test('external seal custody uses separate append-only Python-compatible storage',t=>{
  const dir=temporary(t),store=new SQLiteStore(join(dir,'tree.db')),{root}=tree(store),report=seal(store,root.id);
  assert.throws(()=>new SQLiteSealSink(join(dir,'tree.db')),/must not use a Pollard store/);
  const path=join(dir,'custody.db'),sink=new SQLiteSealSink(path),options={storeId:'audit',signerIdentity:'operator',sealedAt:'2026-10-08T00:00:00Z'};
  assert.equal(sink.publish(report,options).sequence,1); assert.equal(sink.publish(report,options).sequence,2); sink.close();
  const reopened=new SQLiteSealSink(path); assert.equal(reopened.records().length,2); assert.equal(reopened.records()[0].digest,report.digest); reopened.close(); store.close();
});

test('Python and npm read each other SQLite recordings, sealed manifests and custody records',t=>{
  const available=spawnSync('python',['-c','import sys; print(sys.version)'],{encoding:'utf8'}); if(available.error || available.status!==0) return t.skip('Python interpreter unavailable');
  const dir=temporary(t),python=String.raw`
import json, sys
from pathlib import Path
from pollard import SQLiteStore, export_subtree, import_subtree, seal
from pollard.tree import Node
from pollard.seal_custody import SQLiteSealSink
p=Path(sys.argv[1])
with SQLiteStore(p/'python.db', intern_threshold=8) as s:
 root=Node.make(kind='root',parent=None,payload={'run':'python'})
 s.put(root)
 s.put(Node.make(kind='model_call',parent=root.id,payload={'long':'hello '*20,'literal':{'__pollard_ref':'a'*64}},result={'value':1.0}))
 export_subtree(s,root.id,p/'python.json')
 (p/'python-seal.json').write_text(json.dumps(seal(s,root.id).to_dict()),encoding='utf8')
with SQLiteStore(p/'npm.db',read_only=True) as s:
 root=s.roots()[0]
 assert len(list(s.walk(root)))==4
 assert seal(s,root).to_dict()==json.loads((p/'npm-seal.json').read_text())
with SQLiteStore(p/'imported.db') as s:
 assert import_subtree(p/'npm.json',s).imported==4
assert len(SQLiteSealSink(p/'custody.db').records())==1
`;
  const npm=new SQLiteStore(join(dir,'npm.db')),{root}=tree(npm),report=seal(npm,root.id); exportSubtree(npm,root.id,join(dir,'npm.json')); writeFileSync(join(dir,'npm-seal.json'),JSON.stringify(report)); npm.close();
  const custody=new SQLiteSealSink(join(dir,'custody.db')); custody.publish(report,{storeId:'npm',signerIdentity:'test'}); custody.close();
  const run=spawnSync('python',['-c',python,dir],{encoding:'utf8',env:{...process.env,PYTHONPATH:resolve('../../src')}});
  assert.equal(run.status,0,run.stderr);
  const py=new SQLiteStore(join(dir,'python.db'),{readOnly:true}),pyRoot=py.roots()[0]; assert.deepEqual(seal(py,pyRoot),JSON.parse(readFileSync(join(dir,'python-seal.json'),'utf8')));
  assert.equal([...py.walk(pyRoot)][1].resultText,'{"value":1.0}'); assert.equal(importSubtree(join(dir,'python.json'),new MemoryStore()).imported,2); py.close();
});
