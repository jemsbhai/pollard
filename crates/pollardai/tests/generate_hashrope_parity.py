"""Generate operation-log bytes using the verified release and installed hashrope."""
import dataclasses
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
import hashrope
from pollard import HashRopeStore
from pollard.tree import Node
from pollard.cli import render_ascii
assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__
store = HashRopeStore()
root = Node.make(kind="root", parent=None, payload={"run":"hashrope-\u00e9"})
child = Node.make(kind="model_call", parent=root.id,
    payload={"model":"mock-1", "large":2**128, "text":"\U0001f98a"},
    result={"text":"first", "small":1e-7}, meta={"charges":{"usd":0.3}})
conflict = Node.make(kind="model_call", parent=root.id, payload=child.payload,
    result={"text":"second", "small":1e-6})
leaf = Node.make(kind="note", parent=child.id, payload={"note":"leaf"})
def node_record(node):
    result = dataclasses.asdict(node)
    result["result_text"] = result.pop("_result_text")
    return result
operations = [{"op":"put","node":node_record(node)} for node in (root, child, child, conflict, leaf)]
operations += [{"op":"meta","id":child.id,"patch":{"label":"kept", "fraction":1e16}}]
for operation in operations:
    if operation["op"] == "put": store.put(Node(**{("_result_text" if k == "result_text" else k):v for k,v in operation["node"].items()}))
    else: store.update_meta(operation["id"], operation["patch"])
def snapshot():
    store.validate_log()
    return {"log":store.to_bytes().decode(), "hash":store.content_hash(),
            "ascii":render_ascii(store,root.id), "unicode":render_ascii(store,root.id,unicode=True),
            "ascii_payloads":render_ascii(store,root.id,include_payloads=True),
            "nodes":[node_record(node) for node in store.walk(root.id)]}
snapshots = {"recorded":snapshot()}
imports = []
for separator in ["\n", "\r\n", "\r"]:
    for terminated in [False, True]:
        data = snapshots["recorded"]["log"].replace("\n", separator)
        if not terminated: data = data[:-len(separator)]
        imported = HashRopeStore(data.encode())
        imported.validate_log()
        assert imported.to_bytes() == data.encode()
        imports.append({"log":data,"hash":imported.content_hash()})
store._pollard_compact()
snapshots["compacted"] = snapshot()
store._pollard_drop_nodes({leaf.id})
snapshots["dropped"] = snapshot()
module_root = Path(hashrope.__file__).parent
fixture = {"provenance":{"wheel_sha256":sha,"hashrope_version":importlib.metadata.version("hashrope"),
    "hashrope_source_sha256":{str(p.relative_to(module_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in module_root.rglob("*.py")}},
    "operations":operations, "snapshots":snapshots, "leaf":leaf.id, "imports":imports}
Path(__file__).with_name("pypi160_hashrope.json").write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("Generated hashrope log, hash, duplicate, metadata, compaction and drop fixtures")
