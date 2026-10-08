"""Verify PyPI 1.6.0 and Rust coordinate through the same SQLite ledgers."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
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
from pollard import SQLiteStore
from pollard.arbiter import BudgetReservation, WindowReservation
output=a.output_dir.resolve()
output.mkdir(parents=True,exist_ok=True)
database=output/'arbiter.db'
assert not database.exists(), 'Use a fresh output directory'
request=BudgetReservation('python-scope',{'steps':Decimal(1)},{},{'steps':Decimal(1)})
with SQLiteStore(database) as store:
    assert store._pollard_reserve('python-live',[request],[],60).ok
command=['cargo','run','--quiet','--manifest-path',str(a.crate.resolve()/'Cargo.toml'),'--example','arbitration_interop','--',str(database)]
process=subprocess.run(command,text=True,capture_output=True,check=True)
rust=json.loads(process.stdout)
with SQLiteStore(database) as store:
    blocked=store._pollard_reserve('python-after',[request],[],60)
    assert not blocked.ok and blocked.remaining==Decimal(0)
    cost=BudgetReservation('rust-scope',{'usd':Decimal('0.3')},{},{'usd':Decimal('0.2')})
    assert store._pollard_reserve('python-cost',[cost],[],60).ok
    assert store._pollard_renew('python-cost',60)
    store._pollard_settle('python-cost',{'usd':Decimal('0.2')})
    assert not store._pollard_reserve('python-overshoot',[cost],[],60).ok
    window=WindowReservation('shared-window','requests',Decimal(1),Decimal(1),60)
    refused=store._pollard_reserve('python-window',[],[window],60)
    assert not refused.ok and refused.reason=='window'
    store._pollard_settle('rust-window',{'requests':Decimal(1)})
    assert not store._pollard_reserve('python-window-after',[],[window],60).ok
result={'status':'passed','wheel_sha256':wheel_hash,'python':sys.version,'rust':rust,'checks':['Rust refused active Python budget reservation','Rust settled Python reservation exactly once','Python observed Rust-settled budget charge','Mixed-language decimal charges sum exactly to 0.3','Python renewed shared reservation lease','Python refused active Rust window reservation','Python settled Rust window reservation and retained its sliding-window charge']}
(output/'arbitration-interop-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
