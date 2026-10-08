"""Live pinned Python/native Redis interoperability, isolated UUID namespace.

Set POLLARD_REDIS_TEST_URL to an isolated Redis server. Arguments: wheel, native
redis_interop executable. All user data is scoped to a fresh logical store id.
"""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

wheel=Path(sys.argv[1]).resolve();binary=Path(sys.argv[2]).resolve()
sha="569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sha
sys.path.insert(0,str(wheel))
import pollard
from pollard.stores.redis import RedisStore
from pollard.stores._transactional import _node_text
from pollard.tree import Node
from pollard.arbiter import BudgetReservation,WindowReservation
from pollard.errors import IntegrityError
assert pollard.__version__=="1.6.0" and str(wheel) in pollard.__file__
namespace="python-rust-redis-"+uuid.uuid4().hex
store=RedisStore(os.environ["POLLARD_REDIS_TEST_URL"],store_id=namespace)
def rust(command,*args,ok=True):
    result=subprocess.run([str(binary),command,namespace,*args],capture_output=True,text=True,encoding="utf-8")
    assert (result.returncode==0)==ok,(command,result.stderr)
    return result.stdout.strip()
root=Node.make(kind="root",parent=None,payload={"run":"Python Redis café","huge":2**128})
child=Node.make(kind="model_call",parent=root.id,payload={"model":"test"},result={"text":"雪é","tiny":1e-7,"huge":2**80})
store.put(root);store.put(child)
assert json.loads(rust("inspect",root.id))==[_node_text(n) for n in store.walk(root.id)]
rust_root=rust("seed");assert store.get(rust_root).meta=={"origin":"rust"}
rust_nodes=list(store.walk(rust_root));assert rust_nodes[1].result["text"]=="雪é" and rust_nodes[1].result["float"]==1e-7
b=BudgetReservation(scope_id="interop-scope",limits={"steps":Decimal("2"),"usd":Decimal("0.0000010")},baseline={"steps":Decimal("0"),"usd":Decimal("0.00000000")},estimates={"steps":Decimal("1"),"usd":Decimal("0.0000002")})
w=WindowReservation(ledger_key="interop-window",meter="steps",limit=Decimal("2"),amount=Decimal("1"),window_seconds=10.0)
assert store._pollard_reserve("python",[b],[w],30.0).ok
assert rust("reserve","rust")=="true"
assert rust("reserve","blocked")=="false"
assert store._pollard_reserve("rust",[b],[w],30.0).ok
rust("settle","rust")
store._pollard_settle("rust",{"steps":Decimal("1"),"usd":Decimal("0.00000010")})
try:store._pollard_settle("rust",{"steps":Decimal("2")})
except IntegrityError:pass
else:raise AssertionError("different-charge retry unexpectedly allowed")
store._pollard_release("python");rust("release","python");rust("reserve","python",ok=False)
decimal_forms = ["1E+2", "1.0E+2", "1.00E+2", "-0", "-0.00", "-0E+2", "-0E-7", "0E+9", "1.2300", "2.500E+3", "0.0000010"]
for index, amount in enumerate(decimal_forms):
    reservation = f"text-{index}"
    b = BudgetReservation(scope_id="decimal-scope", limits={"usd": Decimal("1E+6")}, baseline={"usd": Decimal("1E+2")}, estimates={"usd": Decimal(amount)})
    w = WindowReservation(ledger_key="decimal-window", meter="usd", limit=Decimal("1.0E+6"), amount=Decimal(amount), window_seconds=60.0)
    if index % 2 == 0:
        assert store._pollard_reserve(reservation, [b], [w], 60.0).ok
        assert rust("reserve-text", reservation, amount) == "true"
        rust("settle-text", reservation, amount)
        store._pollard_settle(reservation, {"usd": Decimal(amount)})
    else:
        assert rust("reserve-text", reservation, amount) == "true"
        assert store._pollard_reserve(reservation, [b], [w], 60.0).ok
        store._pollard_settle(reservation, {"usd": Decimal(amount)})
        rust("settle-text", reservation, amount)
    rust("settle-text", reservation, "999.00", ok=False)
print(json.dumps({"namespace":namespace,"wheel_sha256":sha,"decimal_text_cases":len(decimal_forms),"checks":["python_to_rust_nodes","rust_to_python_nodes","shared_budget_refusal","cross_language_idempotent_reservation","exact_decimal_settlement_retry","different_charge_refusal","permanent_release_tombstone","bidirectional_decimal_text_retries"]},indent=2))
store.close()
