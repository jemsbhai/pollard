"""Live Python-wheel/native PostgreSQL interoperability, isolated in a new schema.

Requires psycopg, a built --features postgres --example postgres_interop,
and POLLARD_TEST_POSTGRES_DSN pointing to a disposable test database.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from decimal import Decimal
import psycopg

p=argparse.ArgumentParser()
p.add_argument('--wheel',type=Path,required=True)
p.add_argument('--binary',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
sha=hashlib.sha256(a.wheel.read_bytes()).hexdigest()
assert sha=='569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
sys.path.insert(0,str(a.wheel.resolve()))
from pollard import PostgresStore,Runtime,Budget,WindowMeter,BudgetExceeded,seal,verify
from pollard.meters import StepMeter
from pollard.arbiter import BudgetReservation
schema='pollard_interop_'+uuid.uuid4().hex
base=os.environ['POLLARD_TEST_POSTGRES_DSN']
dsn=base+f" options='-c search_path={schema}'"
env=dict(os.environ,POLLARD_TEST_POSTGRES_DSN=dsn)
results={}
def native(store,mode,label):
    proc=subprocess.run([str(a.binary.resolve()),store,mode,label],env=env,capture_output=True,text=True,encoding='utf-8',check=True)
    return json.loads(proc.stdout)
with psycopg.connect(base,autocommit=True) as admin:
    admin.execute(f'CREATE SCHEMA {schema}')
    try:
        with PostgresStore(dsn,store_id='records',intern_threshold=8) as store:
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
            with PostgresStore(dsn,store_id=mode) as store:
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
        with PostgresStore(dsn,store_id='reverse') as store:
            runtime=Runtime(store,meters=[StepMeter(),WindowMeter('requests',3,60)])
            run=runtime.run('reverse')
            for i in range(2):run.model_call({'python':i},fn=lambda _:{'ok':True})
            assert native('reverse','window','reverse')['dispatched']
            try:run.model_call({'python':'after'},fn=lambda _:(_ for _ in ()).throw(AssertionError('provider dispatched')))
            except BudgetExceeded:pass
            else:raise AssertionError('Python ignored settled Rust window charge')
            results['reverse_window']=True
        with PostgresStore(dsn,store_id='settlement') as store:
            req=BudgetReservation('money',{'usd':Decimal('0.3')},{},{'usd':Decimal('0.1')})
            assert store._pollard_reserve('mixed',[req],[],60.0).ok
            native('settlement','settle','mixed')
            store._pollard_settle('mixed',{'usd':Decimal('0.3')})
            assert not store._pollard_reserve('over',[req],[],60.0).ok
            results['mixed_idempotent_settlement']=True
        with psycopg.connect(dsn) as connection:
            assert connection.execute('SELECT count(*) FROM pollard_reservations').fetchone()[0]==0
            assert connection.execute("SELECT count(*) FROM pollard_reservation_state WHERE state='active'").fetchone()[0]==0
    finally:admin.execute(f'DROP SCHEMA {schema} CASCADE')
document={'status':'passed','wheel_sha256':sha,'binary_sha256':hashlib.sha256(a.binary.read_bytes()).hexdigest(),'python':sys.version,'cases':results,'schema_cleaned':True}
a.output.write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'passed','cases':list(results),'output':str(a.output)}))
