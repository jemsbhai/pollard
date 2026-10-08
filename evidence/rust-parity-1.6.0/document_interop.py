"""Exact PyPI 1.6 wheel / native MongoDB or routed Neo4j interoperability.

Uses only a fresh random MongoDB database or Neo4j logical-store prefix. Requires
pymongo / neo4j and the prebuilt document_interop example; never builds implicitly.
"""
import argparse,contextlib,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
from decimal import Decimal
from importlib.metadata import version
p=argparse.ArgumentParser()
p.add_argument('--backend',choices=['mongodb','neo4j'],required=True)
p.add_argument('--wheel',type=Path,required=True)
p.add_argument('--binary',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--python-source',type=Path,help='separate corrected-source Mongo UTC regression; use default client options instead of the frozen-wheel timezone workaround')
a=p.parse_args()
sha=hashlib.sha256(a.wheel.read_bytes()).hexdigest()
assert sha=='569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
if a.python_source:
    if a.backend!='mongodb' or not (a.python_source/'pollard/stores/mongodb.py').is_file():
        p.error('--python-source requires mongodb and a source directory containing pollard/stores/mongodb.py')
    sys.path.insert(0,str(a.python_source.resolve()))
else:
    sys.path.insert(0,str(a.wheel.resolve()))
from pollard import MongoStore,Neo4jStore,Runtime,Budget,WindowMeter,BudgetExceeded,seal,verify
from pollard.meters import StepMeter
from pollard.arbiter import BudgetReservation
prefix='pollard_interop_'+uuid.uuid4().hex
env=dict(os.environ)
results={}
if a.backend=='mongodb':
    import pymongo
    uri=env['POLLARD_TEST_MONGO_URI']
    env['POLLARD_TEST_MONGO_DATABASE']=prefix
    admin=pymongo.MongoClient(uri)
else:
    import neo4j
    uri=env['POLLARD_TEST_NEO4J_URI']
    assert uri.startswith('neo4j://'), 'this check requires actual routing discovery'
    env.setdefault('POLLARD_TEST_NEO4J_USER','neo4j')
    auth=(env['POLLARD_TEST_NEO4J_USER'],env['POLLARD_TEST_NEO4J_PASSWORD'])
    admin=neo4j.GraphDatabase.driver(uri,auth=auth)
def store_id(name): return prefix+'_'+name
@contextlib.contextmanager
def store_for(name):
    cls=MongoStore if a.backend=='mongodb' else Neo4jStore
    kw={'store_id':store_id(name)}
    kw.update({'database':prefix} if a.backend=='mongodb' else {'auth':auth})
    if a.backend=='mongodb' and not a.python_source:
        kw['tz_aware']=True
    with cls(uri,**kw) as store: yield store

def native(store,mode,label):
    proc=subprocess.run([str(a.binary.resolve()),a.backend,store_id(store),mode,label],env=env,capture_output=True,text=True,encoding='utf-8',check=True)
    return json.loads(proc.stdout)
try:
    if a.backend=='mongodb':
        with MongoStore(uri,database=prefix,store_id=store_id('clock-naive')) as clock:
            naive=clock._write(lambda tx:tx.now())-time.time()
        with MongoStore(uri,database=prefix,store_id=store_id('clock-aware'),tz_aware=True) as clock:
            aware=clock._write(lambda tx:tx.now())-time.time()
        assert abs(aware)<5.0
        if a.python_source:
            assert abs(naive)<5.0
        results['python_clock_configuration']={'required_option':None if a.python_source else 'tz_aware=True','default_clock_offset_seconds':naive,'aware_clock_offset_seconds':aware,'reason':'corrected source normalizes naive BSON UTC timestamps' if a.python_source else 'PyPI1.6 default naive BSON datetime.timestamp uses host timezone'}
    with store_for('records') as store:
        run=Runtime(store).run('python-record')
        node=run.model_call({'unicode':'é東京abcdefghij','literal':{'__pollard_ref':'0'*64}},fn=lambda _:{'float':1e-5,'usage':{'input_tokens':2,'output_tokens':3}})
        exported=native('records','export',run.root_id)
        assert exported['seal']['digest']==seal(store,run.root_id).digest
        assert next(n for n in exported['nodes'] if n['id']==node.id)['result']==node.result_text
        rust=native('records','record','rust-record')
        assert rust['dispatched'] and verify(store,rust['node_id']).ok
        assert store.get(rust['node_id']).result['float']==1e-5
        results['exact_records']={'python_seal':exported['seal']['digest'],'rust_node':rust['node_id']}
    for mode in ('budget','window'):
        with store_for(mode) as store:
            meters=[StepMeter()]+([WindowMeter('requests',3,60)] if mode=='window' else [])
            run=Runtime(store,meters=meters).run(mode,budget=Budget(steps=1) if mode=='budget' else None)
            if mode=='window':
                for i in range(2):run.model_call({'python':i},fn=lambda _:{'ok':True})
            observed=[]
            def callback(_):
                value=native(mode,mode,mode)
                assert value['status']=='refused' and not value['dispatched'],value
                observed.append(value)
                return {'ok':True}
            run.model_call({'python':'live'},fn=callback)
            assert native(mode,mode,mode)['status']=='refused'
            results[mode]={'live_python_reservation_refused':True,'observed':observed}
    with store_for('reverse') as store:
        runtime=Runtime(store,meters=[StepMeter(),WindowMeter('requests',3,60)])
        run=runtime.run('reverse')
        for i in range(2):run.model_call({'python':i},fn=lambda _:{'ok':True})
        assert native('reverse','window','reverse')['dispatched']
        try:run.model_call({'python':'after'},fn=lambda _:(_ for _ in ()).throw(AssertionError('provider dispatched')))
        except BudgetExceeded:pass
        else:raise AssertionError('Python ignored settled Rust window charge')
        results['reverse_window']=True
    with store_for('settlement') as store:
        req=BudgetReservation('money',{'usd':Decimal('0.3')},{},{'usd':Decimal('0.1')})
        assert store._pollard_reserve('mixed',[req],[],60.0).ok
        native('settlement','settle','mixed')
        store._pollard_settle('mixed',{'usd':Decimal('0.3')})
        assert not store._pollard_reserve('over',[req],[],60.0).ok
        results['mixed_idempotent_settlement']=True
    with store_for('tiny') as store:
        request=BudgetReservation('small',{'usd':Decimal('3E-8')},{},{'usd':Decimal('1E-8')})
        assert store._pollard_reserve('tiny',[request],[],60.0).ok
        native('tiny','settle-tiny','tiny')
        store._pollard_settle('tiny',{'usd':Decimal('3E-8')})
        assert not store._pollard_reserve('tiny-over',[request],[],60.0).ok
        results['scientific_decimal_wire_digest']=True
finally:
    if a.backend=='mongodb':admin.drop_database(prefix)
    else:
        with admin.session(database='neo4j') as s:
            s.run('MATCH (n) WHERE (n:_PollardKV OR n:_PollardCoordinator) AND n.store_id STARTS WITH $prefix DETACH DELETE n',prefix=prefix+'_').consume()
    admin.close()
crate=Path(__file__).resolve().parents[2]/'crates'/'pollardai'
sources={str(path.relative_to(crate)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((crate/'src').glob('*.rs'))}
for name in ['Cargo.toml','Cargo.lock','examples/document_interop.rs']:
    path=crate/name
    sources[name]=hashlib.sha256(path.read_bytes()).hexdigest()
document={'status':'passed','backend':a.backend,'wheel_sha256':sha,'binary_sha256':hashlib.sha256(a.binary.read_bytes()).hexdigest(),'sources_sha256':sources,'python':sys.version,'python_driver':version('pymongo' if a.backend=='mongodb' else 'neo4j'),'rust_driver':{'mongodb':'2.8.2','neo4j':'0.2.0'}[a.backend],'cases':results,'namespace_cleaned':True,'cluster_failover_tested':False}
document['python_origin']={'kind':'corrected-source','source_sha256':{str(path.relative_to(a.python_source)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((a.python_source/'pollard').rglob('*.py'))}} if a.python_source else {'kind':'frozen-wheel','sha256':sha}
a.output.write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'passed','cases':list(results),'output':str(a.output)}))
