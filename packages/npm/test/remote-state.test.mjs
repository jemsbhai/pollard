import test from 'node:test';
import assert from 'node:assert/strict';
import { createRemoteState, applyRemoteOperation, isRemoteMutation } from '../dist/esm/remote-state.js';
import { Node, IntegrityError } from '../dist/esm/tree.js';
import { resultDigestFromText, resultToText, sha256 } from '../dist/esm/identity.js';

function harness() {
  let state=createRemoteState(),now=1000;
  return {
    get state(){return state;},
    set state(value){state=value;},
    set now(value){now=value;},
    call(method,...args){const response=applyRemoteOperation(state,method,args,now);state=JSON.parse(JSON.stringify(response.state));return response.result;},
  };
}
const request=(amount,limit=1,scopeId='shared')=>[{scopeId,limits:{usd:limit},baseline:{usd:0},estimates:{usd:amount}}];

test('remote state preserves result text and immutable identity across serialization',()=>{
  const remote=harness(),root=Node.make({kind:'root',parent:null,payload:{run:'remote'}});remote.call('put',root.toStorage());
  const child=Node.make({kind:'model_call',parent:root.id,payload:{model:'test'}}),exact='{ "value": 1.00 }';
  const record={...child.toStorage(),result_text:exact,result_digest:resultDigestFromText(exact)};
  remote.call('put',record);assert.equal(remote.call('get',child.id).result_text,exact);
  remote.call('put',Node.make({kind:child.kind,parent:child.parent,payload:child.payload,result:{value:2}}).toStorage());
  remote.call('put',Node.make({kind:child.kind,parent:child.parent,payload:child.payload,result:{value:2}}).toStorage());
  assert.equal(remote.call('get',child.id).meta.result_conflicts.length,1);
  assert.deepEqual(remote.call('walk',root.id).map(node=>node.id),[root.id,child.id]);
  const detached=remote.call('get',child.id);detached.meta.changed=true;assert.equal(remote.call('get',child.id).meta.changed,undefined);
});

test('remote claim ownership survives reload and competing owners cannot finalize',()=>{
  const remote=harness(),root=Node.make({kind:'root',parent:null,payload:{run:'claim'}});remote.call('put',root.toStorage());
  const pending=Node.make({kind:'tool_call',parent:root.id,payload:{tool:'charge'},meta:{state:'pending'}});
  assert.equal(remote.call('claim',pending.toStorage(),'first'),true);assert.equal(remote.call('claim',pending.toStorage(),'second'),false);
  const final=Node.make({kind:pending.kind,parent:pending.parent,payload:pending.payload,result:{sent:true},meta:{state:'completed'}});
  const before=JSON.stringify(remote.state);assert.throws(()=>remote.call('finalize',final.toStorage(),'second'),/owner/);assert.equal(JSON.stringify(remote.state),before);
  remote.call('finalize',final.toStorage(),'first');assert.throws(()=>remote.call('finalize',final.toStorage(),'first'),/owner/);
  assert.equal(remote.call('get',pending.id).meta.state,'completed');
});

test('remote metadata completion revokes pending owner and deletion enforces subtree integrity',()=>{
  const remote=harness(),root=Node.make({kind:'root',parent:null,payload:{run:'delete'}}),pending=Node.make({kind:'note',parent:root.id,payload:{},meta:{state:'pending'}});
  remote.call('put',root.toStorage());remote.call('claim',pending.toStorage(),'owner');remote.call('updateMeta',pending.id,{state:'failed'});
  assert.equal(Object.hasOwn(remote.state.owners,pending.id),false);assert.throws(()=>remote.call('dropNodes',[root.id]),/retaining its children/);
  remote.call('dropNodes',[root.id,pending.id]);assert.deepEqual(remote.call('roots'),[]);
});

test('remote reservations retry idempotently and exact decimal settlement is never charged twice',()=>{
  const remote=harness();
  assert.equal(remote.call('pollardReserve','a',request(0.1,0.3),[],30).ok,true);
  assert.equal(remote.call('pollardReserve','a',request(0.1,0.3),[],30).ok,true);
  assert.throws(()=>remote.call('pollardReserve','a',request(0.2,0.3),[],30),/changed request/);
  assert.equal(remote.call('pollardReserve','b',request(0.2,0.3),[],30).ok,true);
  assert.equal(remote.call('pollardReserve','c',request(0.01,0.3),[],30).ok,false);
  remote.call('pollardSettle','a',{usd:0.1});remote.call('pollardSettle','a',{usd:0.1});remote.call('pollardSettle','b',{usd:0.2});
  assert.equal(remote.state.budget['["shared","usd"]'],'0.3');
  assert.throws(()=>remote.call('pollardSettle','a',{usd:0.2}),/different charges/);
  assert.throws(()=>remote.call('pollardReserve','a',request(0.1,0.3),[],30),/already settled/);
  assert.equal(remote.call('pollardReserve','zero',request(0,0.3),[],30).ok,true);
});

test('remote release and expiry tombstones prevent replaying old admission after compaction',()=>{
  const remote=harness();remote.call('pollardReserve','release',request(1),[],10);remote.call('pollardRelease','release');remote.call('pollardRelease','release');
  assert.throws(()=>remote.call('pollardReserve','release',request(1),[],10),/already released/);
  remote.call('pollardReserve','expiry',request(1),[],10);remote.now=1011;
  assert.equal(remote.call('pollardRenew','expiry',10),false);assert.throws(()=>remote.call('pollardReserve','expiry',request(1),[],10),/expired/);
  assert.equal(remote.call('pollardReserve','new',request(1),[],10).ok,true);
  remote.call('compact');assert.equal(Object.keys(remote.state.reservations).length,3);
  assert.throws(()=>remote.call('pollardSettle','missing',{usd:1}),/unknown reservation/);
});

test('remote renewal uses authoritative time and preserves existing active reservations',()=>{
  const remote=harness();remote.call('pollardReserve','a',request(1),[],10);remote.now=1009;assert.equal(remote.call('pollardRenew','a',10),true);
  remote.now=1011;assert.equal(remote.call('pollardReserve','b',request(1),[],10).ok,false);
  remote.now=1020;assert.equal(remote.call('pollardReserve','b',request(1),[],10).ok,true);
});

test('remote window admission counts pending and settled charges per meter until expiration',()=>{
  const remote=harness(),window=[{ledgerKey:'rate',meter:'calls',amount:1,limit:1,windowSeconds:60}];
  assert.equal(remote.call('pollardReserve','a',[],window,30).ok,true);assert.equal(remote.call('pollardReserve','b',[],window,30).reason,'window');
  remote.call('pollardSettle','a',{calls:1});assert.equal(remote.call('pollardReserve','b',[],window,30).ok,false);
  assert.equal(remote.call('pollardReserve','other-meter',[],[{...window[0],meter:'other'}],30).ok,true);
  remote.now=1061;assert.equal(remote.call('pollardReserve','b',[],window,30).ok,true);
  assert.equal(Object.keys(remote.state.windowEvents).length,0);
});

test('remote failures cannot partially mutate state and malformed schemas fail closed',()=>{
  const remote=harness(),before=JSON.stringify(remote.state);
  assert.throws(()=>remote.call('pollardReserve','invalid',request(-1),[],30),/nonnegative/);assert.equal(JSON.stringify(remote.state),before);
  assert.throws(()=>applyRemoteOperation({...createRemoteState(),version:2},'roots',[],1000),/schema version/);
  assert.throws(()=>applyRemoteOperation({...createRemoteState(),budget:{x:'NaN'}},'roots',[],1000),IntegrityError);
  assert.throws(()=>isRemoteMutation('__proto__'),/unsupported/);
  assert.equal(isRemoteMutation('get'),false);assert.equal(isRemoteMutation('claim'),true);
  remote.call('pollardReserve','__proto__',request(0.1),[],30);remote.call('pollardSettle','__proto__',{usd:0.1});assert.equal(Object.getPrototypeOf(remote.state.reservations),Object.prototype);
});

test('remote offline commit batches require unchanged snapshots and roll back all prior mutations on failure',()=>{
  const remote=harness(),root=Node.make({kind:'root',parent:null,payload:{run:'atomic'}}),snapshot=remote.call('snapshot'),digest=sha256(resultToText(snapshot));
  const operations=[{method:'put',args:[root.toStorage()]},{method:'updateMeta',args:[root.id,{ready:true}]}];
  remote.call('commitBatch',digest,operations);assert.equal(remote.call('get',root.id).meta.ready,true);
  assert.throws(()=>remote.call('commitBatch',digest,operations),/snapshot changed/);
  const before=JSON.stringify(remote.state),current=sha256(resultToText(remote.call('snapshot')));
  assert.throws(()=>remote.call('commitBatch',current,[{method:'updateMeta',args:[root.id,{shouldRollback:true}]},{method:'updateMeta',args:['missing',{bad:true}]}]),/missing/);
  assert.equal(JSON.stringify(remote.state),before);
  assert.throws(()=>remote.call('commitBatch',current,[{method:'pollardReserve',args:['a',request(1),[],30]}]),/offline store mutations/);
});

test('remote custom meters named after Object prototype fields count absent amounts as zero',()=>{
  const remote=harness(),limits=JSON.parse('{"constructor":1,"__proto__":1,"toString":1}');
  assert.equal(remote.call('pollardReserve','names',[{scopeId:'named',limits,baseline:{},estimates:{}}],[],30).ok,true);
  remote.call('pollardSettle','names',{});
  for(const name of Object.keys(limits)) assert.equal(remote.state.budget[JSON.stringify(['named',name])],'0');
});
