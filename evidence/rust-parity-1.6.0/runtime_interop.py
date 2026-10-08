"""Verify actual Python and Rust Runtime calls contend on the same SQLite scope/window.

Each Rust probe runs inside a live Python model callback, before Python records
its result. This proves enforcement uses shared pending reservations.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

p=argparse.ArgumentParser()
p.add_argument('--wheel',type=Path,required=True)
p.add_argument('--crate',type=Path,required=True)
p.add_argument('--output-dir',type=Path,required=True)
a=p.parse_args()
wheel=a.wheel.resolve()
wheel_hash=hashlib.sha256(wheel.read_bytes()).hexdigest()
assert wheel_hash=='569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
sys.path.insert(0,str(wheel))
from pollard import Budget, Runtime, SQLiteStore, WindowMeter
from pollard.meters import StepMeter
crate=a.crate.resolve()
subprocess.run(['cargo','build','--quiet','--manifest-path',str(crate/'Cargo.toml'),'--example','runtime_interop'],check=True)
binary=crate/'target/debug/examples'/('runtime_interop.exe' if sys.platform=='win32' else 'runtime_interop')
output=a.output_dir.resolve()
output.mkdir(parents=True,exist_ok=True)
cases={}
result={'text':'python result','usage':{'input_tokens':0,'output_tokens':0}}

def probe(path,mode,label,expected_dispatched=False):
    process=subprocess.run([str(binary),str(path),mode,label],text=True,capture_output=True,check=True)
    value=json.loads(process.stdout)
    assert value['dispatched']==expected_dispatched and value['status']==('completed' if expected_dispatched else 'refused'),value
    return value

for mode in ('window','budget','branch'):
    database=output/f'{mode}.db'
    assert not database.exists(), 'Use a fresh output directory'
    label=f'runtime-interop-{mode}'
    meters=[StepMeter()]
    if mode=='window':meters.append(WindowMeter('requests',3,60))
    with SQLiteStore(database) as store:
        runtime=Runtime(store,meters=meters)
        run=runtime.run(label,budget=None if mode=='window' else Budget(steps=10 if mode=='branch' else 1))
        root=run.root_id
        if mode=='branch':
            context=run.branch(budget=Budget(steps=1))
            run=context.__enter__()
        anchor=run.cursor_id
        if mode=='window':
            for step in range(2):run.model_call({'source':'python','step':step},fn=lambda _:result)
        observed=[]
        def live_call(_):
            with sqlite3.connect(database) as conn:
                reservations=conn.execute('SELECT kind,scope_id,meter,amount FROM reservations ORDER BY kind,scope_id,meter').fetchall()
            assert reservations,reservations
            rust=probe(database,mode,label)
            assert rust['root_id']==root
            if mode=='branch':assert rust['anchor_id']==anchor
            if mode=='window':
                expected_key=meters[-1].ledger_key(root)
                assert rust['window_key']==expected_key
                assert {row[1] for row in reservations if row[0]=='window'}=={expected_key}
            else:
                assert {row[1] for row in reservations if row[0]=='budget'}==({root,anchor} if mode=='branch' else {root})
            observed.append({'rust':rust,'active_python_reservations':reservations})
            return result
        run.model_call({'source':'python','step':'live'},fn=live_call)
        after=probe(database,mode,label)
        with sqlite3.connect(database) as conn:
            assert conn.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0
        cases[mode]={'during_python_callback':observed,'after_python_settlement':after,'python_root':root,'python_anchor':anchor}
        if mode=='branch':context.__exit__(None,None,None)

# Prove native Rust runtime settlement consumes the very same window in Python.
database=output/'window-reverse.db'
label='runtime-interop-window-reverse'
assert not database.exists()
with SQLiteStore(database) as store:
    runtime=Runtime(store,meters=[StepMeter(),WindowMeter('requests',3,60)])
    run=runtime.run(label)
    for step in range(2):run.model_call({'source':'python','step':step},fn=lambda _:result)
    rust=probe(database,'window',label,expected_dispatched=True)
    attempted=[]
    from pollard import BudgetExceeded
    try:
        runtime.run(label).model_call({'source':'python','step':'after-rust'},fn=lambda _:attempted.append(True) or result)
    except BudgetExceeded:
        pass
    else:
        raise AssertionError('Python runtime ignored settled Rust window charge')
    assert not attempted
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0
        events=conn.execute('SELECT scope_id,meter,amount FROM window_events').fetchall()
        assert len(events)==3 and {row[0] for row in events}=={rust['window_key']}
    cases['window_reverse']={'rust':rust,'window_events':events,'python_dispatch_refused':True}

evidence={'status':'passed','wheel_sha256':wheel_hash,'python':sys.version,'checks':['Rust Runtime shares exact Python integer-limit window ledger key','Rust Runtime refuses a live Python window reservation before dispatch','Rust Runtime observes settled Python window usage','Python Runtime refuses after native Rust runtime exhausts shared window','Root budget scope identity matches across runtimes','Branch and ancestor budget scopes match across runtimes','Completed mixed-runtime probes leave no reservation rows'],'cases':cases}
(output/'runtime-interop-result.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
print(json.dumps({'status':'passed','cases':list(cases),'output':str(output/'runtime-interop-result.json')},indent=2))
