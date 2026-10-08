"""Frozen Pollard 1.6.0 shared transactional ledger wire fixtures, no services."""
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
wheel=Path(sys.argv[1]).resolve()
sha="569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sha
sys.path.insert(0,str(wheel))
import pollard
from pollard.stores._transactional import TransactionalKVStore, _reservation_request, _reservation_charges, _node_text
from pollard.arbiter import BudgetReservation, WindowReservation
from pollard.tree import Node
assert pollard.__version__=="1.6.0" and str(wheel) in pollard.__file__
class Tx:
    def __init__(self,values,now):self.values=values;self.time=now
    def get(self,b,k):return self.values.get(b,{}).get(k)
    def items(self,b):return sorted(self.values.get(b,{}).items())
    def put(self,b,k,v):self.values.setdefault(b,{})[k]=v
    def delete(self,b,k):self.values.get(b,{}).pop(k,None)
    def now(self):return self.time
class Memory(TransactionalKVStore):
    def __init__(self):self.values={"schema":{"version":"1"}};self.time=1000.0
    def _read(self,callback):return callback(Tx(self.values,self.time))
    def _write(self,callback):
        values=copy.deepcopy(self.values);result=callback(Tx(values,self.time));self.values=values;return result
    def _is_connection_error(self,error):return False
    def reconnect(self):pass
def decmap(value):return {k:Decimal(v) for k,v in value.items()}
def budget(v):return BudgetReservation(scope_id=v["scope_id"],limits=decmap(v["limits"]),baseline=decmap(v["baseline"]),estimates=decmap(v["estimates"]))
def window(v):return WindowReservation(**{**v,"amount":Decimal(v["amount"]),"limit":Decimal(v["limit"])})
fixtures={"provenance":{"pollard_version":"1.6.0","wheel_sha256":sha,"generator":Path(__file__).name},"requests":[],"nodes":[],"trace":[]}
for amount in ["0","0.00","0.0000001","0.00000010","1.2300","12345678901234567890.000001"]:
    b={"scope_id":"scope:é","limits":{"usd":"100000000000000000000"},"baseline":{"usd":"0.00"},"estimates":{"usd":amount}}
    w={"ledger_key":"window:☃","meter":"usd","limit":"100000000000000000000","amount":amount,"window_seconds":10.0}
    request,digest=_reservation_request([budget(b)],[window(w)],30.0);charges,cdigest=_reservation_charges({"usd":Decimal(amount)})
    fixtures["requests"].append({"budgets":[b],"windows":[w],"lease":30.0,"request":request,"digest":digest,"charges":{"usd":amount},"charges_text":charges,"charges_digest":cdigest})
root=Node.make(kind="root",parent=None,payload={"run":"KV café","integer":2**128},meta={"meta":1e-7})
child=Node.make(kind="model_call",parent=root.id,payload={"model":"test"},result={"text":"é","float":1e-7,"large":2**80},meta={})
for node in [root,child]:fixtures["nodes"].append({"node_text":_node_text(node)})
store=Memory();b={"scope_id":"root","limits":{"steps":"3","usd":"0.0000010","depth":"5"},"baseline":{"steps":"0","usd":"0.00000000"},"estimates":{"steps":"1","usd":"0.0000002"}}
w={"ledger_key":"root-window","meter":"steps","limit":"2","amount":"1","window_seconds":10.0}
actions=[{"op":"reserve","id":"a","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"reserve","id":"a","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"reserve","id":"b","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"reserve","id":"blocked","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"settle","id":"a","charges":{"steps":"1","usd":"0.00000010"}},
    {"op":"settle","id":"a","charges":{"steps":"1","usd":"0.00000010"}},
    {"op":"settle","id":"a","charges":{"steps":"2"}},
    {"op":"release","id":"b"},{"op":"release","id":"b"},
    {"op":"reserve","id":"b","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"reserve","id":"c","budgets":[b],"windows":[w],"lease":3.0},
    {"op":"advance","seconds":3.0},{"op":"renew","id":"c","lease":30.0},
    {"op":"reserve","id":"c","budgets":[b],"windows":[w],"lease":3.0},
    {"op":"settle","id":"c","charges":{"steps":"1","usd":"0.00000020"}},
    {"op":"advance","seconds":10.0},{"op":"reserve","id":"d","budgets":[b],"windows":[w],"lease":30.0},
    {"op":"renew","id":"d","lease":50.0}, {"op":"release","id":"missing"}]
for action in actions:
    row={"action":action}
    try:
        op=action["op"]
        if op=="reserve":
            check=store._pollard_reserve(action["id"],[budget(v) for v in action["budgets"]],[window(v) for v in action["windows"]],action["lease"])
            row["check"]={"ok":check.ok,"reason":check.reason,"meter":check.meter,"requested":str(check.requested),"remaining":str(check.remaining),"window_seconds":check.window_seconds}
        elif op=="settle":store._pollard_settle(action["id"],decmap(action["charges"]))
        elif op=="release":store._pollard_release(action["id"])
        elif op=="renew":row["renewed"]=store._pollard_renew(action["id"],action["lease"])
        else:store.time+=action["seconds"]
    except Exception as exc:row["error"]=type(exc).__name__
    row["state"]=copy.deepcopy(store.values);fixtures["trace"].append(row)
Path(__file__).with_name("pypi160_kv.json").write_text(json.dumps(fixtures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("generated",len(fixtures["requests"]),"requests,",len(fixtures["trace"]),"transaction steps")
