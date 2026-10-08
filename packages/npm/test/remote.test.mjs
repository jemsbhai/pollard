import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { Worker } from 'node:worker_threads';
import { fileURLToPath } from 'node:url';
import { Node, MemoryStore } from '../dist/esm/tree.js';
import { Runtime } from '../dist/esm/runtime.js';
import { resultDigestFromText } from '../dist/esm/identity.js';
import { PostgresStore, RedisStore, MongoStore, Neo4jStore, KafkaStore } from '../dist/esm/remote.js';
import { gc, merge } from '../dist/esm/governance.js';
import { seal } from '../dist/esm/seal.js';

const configurations=[
  {name:'postgres',Class:PostgresStore,url:process.env.POLLARD_NPM_POSTGRES_URL},
  {name:'redis',Class:RedisStore,url:process.env.POLLARD_NPM_REDIS_URL},
  {name:'mongodb',Class:MongoStore,url:process.env.POLLARD_NPM_MONGODB_URL},
  {name:'neo4j',Class:Neo4jStore,url:process.env.POLLARD_NPM_NEO4J_URL,options:{username:process.env.POLLARD_NPM_NEO4J_USERNAME??'neo4j',password:process.env.POLLARD_NPM_NEO4J_PASSWORD}},
  {name:'kafka',Class:KafkaStore,url:process.env.POLLARD_NPM_KAFKA_BROKERS},
];
function configFor(config,storeId,extra={}) {
  return {storeId,timeoutMs:30_000,...config.options,...(config.name==='kafka'?{topic:`pollard-npm-${storeId}`,brokers:config.url.split(',')}:{}),...extra};
}
function open(config,storeId,extra={}) { const options=configFor(config,storeId,extra);return config.name==='kafka'?new config.Class(options):new config.Class(config.url,options); }
function id(){return randomUUID().replaceAll('-','');}
const budget=amount=>[{scopeId:'shared-budget',limits:{usd:0.3},baseline:{usd:0},estimates:{usd:amount}}];
const cliPath=fileURLToPath(new URL('../dist/esm/cli.js',import.meta.url));
function remoteSpec(config,storeId) {
  const environment={...process.env,POLLARD_LIVE_URL:config.url};
  if(config.name==='postgres')return {spec:`pg-env:POLLARD_LIVE_URL#${storeId}`,environment};
  if(config.name==='redis')return {spec:`redis-env:POLLARD_LIVE_URL#${storeId}`,environment};
  if(config.name==='mongodb')return {spec:`mongo-env:POLLARD_LIVE_URL#${storeId}`,environment};
  if(config.name==='neo4j')return {spec:`neo4j-env:POLLARD_LIVE_URL?user-env=POLLARD_LIVE_USER&password-env=POLLARD_LIVE_PASSWORD#${storeId}`,environment:{...environment,POLLARD_LIVE_USER:config.options.username,POLLARD_LIVE_PASSWORD:config.options.password}};
  return {spec:`kafka-env:POLLARD_LIVE_URL?topic=pollard-npm-${storeId}#${storeId}`,environment:{...environment,POLLARD_LIVE_URL:JSON.stringify({brokers:config.url.split(',')})}};
}
async function alterNamespace(config,storeId,state) {
  if(config.name==='postgres') {
    const {Client}=await import('pg'),client=new Client({connectionString:config.url});await client.connect();
    try{if(state===null)await client.query('DELETE FROM pollard_npm_state WHERE store_id=$1',[storeId]);else await client.query('UPDATE pollard_npm_state SET state=$2 WHERE store_id=$1',[storeId,JSON.stringify(state)]);}finally{await client.end();}
  } else if(config.name==='redis') {
    const {createClient}=await import('redis'),client=createClient({url:config.url});client.on('error',()=>{});await client.connect();
    const key=`{pollard-npm-${createHash('sha256').update(storeId).digest('hex')}}:pollard:state`;
    try{if(state===null)await client.del(key);else await client.set(key,JSON.stringify(state));}finally{await client.quit();}
  } else if(config.name==='mongodb') {
    const {MongoClient}=await import('mongodb'),client=new MongoClient(config.url);await client.connect();
    try{const collection=client.db('pollard').collection('pollard_npm_state');if(state===null)await collection.deleteOne({_id:storeId});else await collection.updateOne({_id:storeId},{$set:{state:JSON.stringify(state)}});}finally{await client.close();}
  } else {
    const {default:neo4j}=await import('neo4j-driver'),driver=neo4j.driver(config.url,neo4j.auth.basic(config.options.username,config.options.password)),session=driver.session();
    try{await session.run(state===null?'MATCH(s:PollardNpmStore{id:$id}) DELETE s':'MATCH(s:PollardNpmStore{id:$id}) SET s.state=$state',{id:storeId,state:JSON.stringify(state)});}finally{await session.close();await driver.close();}
  }
}

async function concurrent(config,storeId,method,count=6,record) {
  const gate=new SharedArrayBuffer(4),workers=[];
  const source=`
    const {parentPort,workerData}=require('node:worker_threads');
    const {randomUUID}=require('node:crypto');
    const classes=require(workerData.remotePath),{Node}=require(workerData.treePath);
    const options=workerData.options;
    const Store=classes[workerData.className];
    let store;
    try {
      store=workerData.backend==='kafka'?new Store(options):new Store(workerData.url,options);
      parentPort.postMessage({type:'ready'});
      Atomics.wait(new Int32Array(workerData.gate),0,0);
      let won;
      if(workerData.method==='reserve') {
        const reservation=randomUUID(),budget=[{scopeId:'shared-budget',limits:{usd:0.3},baseline:{usd:0},estimates:{usd:0.1}}];
        won=store.pollardReserve(reservation,budget,[],30).ok;
        if(won) store.pollardSettle(reservation,{usd:0.1});
      } else {
        const pending=Node.fromStorage(workerData.record);won=store.claim(pending);
        if(won) store.finalize(Node.make({kind:pending.kind,parent:pending.parent,payload:pending.payload,result:{done:true},meta:{state:'completed'}}));
      }
      store.close();parentPort.postMessage({type:'done',won});
    } catch(error) {try{store?.close();}catch{} parentPort.postMessage({type:'error',message:error.stack});}
  `;
  const ready=[],done=[];
  for(let n=0;n<count;n++) {
    const worker=new Worker(source,{eval:true,workerData:{remotePath:fileURLToPath(new URL('../dist/cjs/remote.js',import.meta.url)),treePath:fileURLToPath(new URL('../dist/cjs/tree.js',import.meta.url)),className:config.Class.name,backend:config.name,url:config.url,options:configFor(config,storeId,{create:false}),gate,method,record}});
    workers.push(worker);
    let resolveReady,rejectReady,resolveDone,rejectDone;
    ready.push(new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;}));
    done.push(new Promise((resolve,reject)=>{resolveDone=resolve;rejectDone=reject;}));
    worker.on('message',message=>{if(message.type==='ready')resolveReady();else if(message.type==='done')resolveDone(message.won);else{const error=new Error(message.message);rejectReady(error);rejectDone(error);}});
    worker.on('error',error=>{rejectReady(error);rejectDone(error);});
    worker.on('exit',code=>{if(code!==0){const error=new Error(`integration worker exited ${code}`);rejectReady(error);rejectDone(error);}});
  }
  // Attach rejection handlers immediately, including while peers connect.
  const allReady=Promise.all(ready),allDone=Promise.all(done);allDone.catch(()=>{});
  try {await allReady;Atomics.store(new Int32Array(gate),0,1);Atomics.notify(new Int32Array(gate),0,count);return await allDone;}
  finally {await Promise.all(workers.map(worker=>worker.terminate()));}
}

for(const config of configurations) {
  test(`${config.name}: real service recording, reopening, replay and invalid namespace isolation`,{skip:!config.url,timeout:120_000},()=>{
    const storeId=id(),store=open(config,storeId);let reopened;
    try {
      const root=Node.make({kind:'root',parent:null,payload:{run:'remote-live'}});store.put(root);
      const base=Node.make({kind:'model_call',parent:root.id,payload:{model:'test',prompt:'π'.repeat(128)}}),text='{ "value": 1.00, "text": "stored" }';
      const child=Node.fromStorage({...base.toStorage(),result_text:text,result_digest:resultDigestFromText(text),meta:{state:'completed'}});store.put(child);store.updateMeta(child.id,{audited:true});
      const before=seal(store,root.id);store.close();reopened=open(config,storeId,{create:false});
      assert.equal(reopened.get(child.id).resultText,text);assert.equal(reopened.get(child.id).meta.audited,true);assert.deepEqual(seal(reopened,root.id),before);
      const replay=new Runtime({store:reopened,mode:'replay'}).run('remote-live');assert.equal(replay.modelCall(child.payload,()=>{throw Error('replay dispatched handler');}).id,child.id);
      if(config.name!=='kafka') assert.throws(()=>open(config,id(),{create:false}),/namespace does not exist/);
      else assert.equal(typeof reopened.pollardReserve,'undefined');
      assert.throws(()=>reopened.get('f'.repeat(64)),/ffff/);assert.equal(reopened.get(child.id).id,child.id);
    } finally {store.close();reopened?.close();}
  });

  test(`${config.name}: racing dispatch claims produce one owner across native workers`,{skip:!config.url,timeout:120_000},async()=>{
    const storeId=id(),store=open(config,storeId);
    try {
      const root=Node.make({kind:'root',parent:null,payload:{run:'race'}});store.put(root);
      const pending=Node.make({kind:'tool_call',parent:root.id,payload:{tool:'once'},meta:{state:'pending'}});
      const results=await concurrent(config,storeId,'claim',3,pending.toStorage());assert.equal(results.filter(Boolean).length,1);assert.equal(store.get(pending.id).meta.state,'completed');
    } finally {store.close();}
  });

  test(`${config.name}: environment-backed CLI runs, report, verify, export, import and merge`,{skip:!config.url,timeout:180_000},t=>{
    const tempBase=resolve(tmpdir()),directory=mkdtempSync(join(tempBase,'pollard-remote-cli-'));t.after(()=>{assert.equal(dirname(resolve(directory)),tempBase);rmSync(directory,{recursive:true,force:true});});
    const storeId=id(),store=open(config,storeId),run=new Runtime({store}).run('remote-cli');
    const node=run.modelCall({model:'mock'},()=>({text:'recorded',usage:{input_tokens:2,output_tokens:3}}));store.close();
    const {spec,environment}=remoteSpec(config,storeId),cli=(...args)=>{const response=spawnSync(process.execPath,[cliPath,...args],{encoding:'utf8',env:environment,timeout:45_000});assert.equal(response.status,0,response.stderr||response.error?.message);return JSON.parse(response.stdout);};
    assert.equal(cli('runs',spec)[0].id,run.rootId);assert.equal(cli('report',spec,run.rootId).spent.tokens,5);assert.equal(cli('verify',spec).ok,true);
    const manifest=join(directory,'subtree.json');assert.equal(cli('export',spec,run.rootId,manifest).nodes,2);
    const targetId=id(),targetSpec=remoteSpec(config,targetId).spec;assert.equal(cli('import',manifest,targetSpec).imported,2);
    const target=open(config,targetId,{create:false});try{assert.equal(target.get(node.id).resultText,node.resultText);}finally{target.close();}
    const destination=config.name==='kafka'?join(directory,'merge.db'):remoteSpec(config,id()).spec;
    assert.equal(cli('merge','--into',destination,spec)[0].copied,2);
  });

  if(config.name==='kafka') continue;
  test(`${config.name}: namespace deletion and corruption fail closed without silently recreating state`,{skip:!config.url,timeout:120_000},async()=>{
    const storeId=id(),store=open(config,storeId),root=Node.make({kind:'root',parent:null,payload:{run:'failure'}});store.put(root);
    try {
      await alterNamespace(config,storeId,{version:999,nodes:{},owners:{},budget:{},reservations:{},windowEvents:{}});
      assert.throws(()=>store.get(root.id),/schema version/);assert.throws(()=>store.put(root),/schema version/);
      await alterNamespace(config,storeId,null);assert.throws(()=>store.get(root.id),/namespace does not exist/);
      assert.throws(()=>store.pollardReserve('lost',budget(0.1),[],30),/namespace does not exist/);
      assert.throws(()=>open(config,storeId,{create:false}),/namespace does not exist/);
    } finally {store.close();}
  });
  test(`${config.name}: concurrent decimal budgets never overspend and settlement retry is idempotent`,{skip:!config.url,timeout:120_000},async()=>{
    const storeId=id(),store=open(config,storeId);
    try {
      const results=await concurrent(config,storeId,'reserve');assert.equal(results.filter(Boolean).length,3);
      assert.equal(store.pollardReserve('none-left',budget(0.01),[],30).ok,false);
      const request=[{scopeId:'retry',limits:{usd:0.3},baseline:{usd:0},estimates:{usd:0.1}}];
      assert.equal(store.pollardReserve('retry',request,[],30).ok,true);assert.equal(store.pollardReserve('retry',request,[],30).ok,true);
      store.pollardSettle('retry',{usd:0.1});store.pollardSettle('retry',{usd:0.1});assert.throws(()=>store.pollardSettle('retry',{usd:0.2}),/different charges/);
      assert.throws(()=>store.pollardReserve('retry',request,[],30),/already settled/);
    } finally {store.close();}
  });

  test(`${config.name}: native worker renews a lease while the calling thread is blocked`,{skip:!config.url,timeout:120_000},()=>{
    const storeId=id(),first=open(config,storeId),second=open(config,storeId,{create:false});
    try {
      const window=[{ledgerKey:'calls',meter:'calls',amount:1,limit:1,windowSeconds:60}];
      assert.equal(first.pollardReserve('long-call',[],window,0.3).ok,true);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,1000);
      assert.equal(second.pollardReserve('contender',[],window,0.3).ok,false);
      first.pollardRelease('long-call');assert.equal(second.pollardReserve('contender',[],window,1).ok,true);second.pollardRelease('contender');
    } finally {first.close();second.close();}
  });

  test(`${config.name}: atomic merge rollback, stale transaction rejection and offline GC`,{skip:!config.url,timeout:120_000},()=>{
    const storeId=id(),first=open(config,storeId),second=open(config,storeId,{create:false});
    try {
      const source=new MemoryStore(),root=Node.make({kind:'root',parent:null,payload:{run:'merge'}}),child=Node.make({kind:'note',parent:root.id,payload:{pruned:true},meta:{pruned:true}});source.put(root);source.put(child);
      assert.equal(merge(first,source,{requireAtomic:true}).copied,2);
      assert.throws(()=>first.transaction(()=>{first.updateMeta(root.id,{rollback:true});throw Error('rollback');}),/rollback/);assert.equal(first.get(root.id).meta.rollback,undefined);
      assert.throws(()=>first.transaction(()=>{first.updateMeta(root.id,{stale:true});second.updateMeta(root.id,{concurrent:true});}),/snapshot changed/);
      assert.equal(first.get(root.id).meta.stale,undefined);assert.equal(first.get(root.id).meta.concurrent,true);
      assert.deepEqual(gc(first).removedNodes,[child.id]);assert.equal(first.exists(root.id),true);assert.equal(first.exists(child.id),false);
    } finally {first.close();second.close();}
  });
}
