"""Generate CLI tree documents from the SHA-pinned PyPI 1.6.0 wheel."""
import dataclasses
import hashlib
import json
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
from pollard.cli import tree_document, render_ascii, _label
from pollard.redaction import redact
from pollard.store import MemoryStore
from pollard.tree import Node

assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__
store = MemoryStore()
root = Node.make(kind="root", parent=None, payload={"run": None})
store.put(root)
for kind, payload, meta in [
    ("note", {"branch": True}, {}),
    ("note", {"label": "café 🦊"}, {"pruned": True}),
    ("note", {"checkpoint": 2**80}, {"pruned": 1}),
    ("note", {"status": False}, {"charges": [1, 2]}),
    ("model_call", {"modelId": "demo"}, {"charges": {"tokens": 3, "usd": 0.1, "flag": True, "private": "hidden"}, "avoided": {"tokens": 4}}),
    ("model_call", {"model": None, "modelId": "ignored"}, {}),
    ("tool_call", {"tool": "echo", "arguments": {"secret": redact("private")}}, {}),
    ("tool_call", {"tool": [1, 2]}, {}),
    ("refusal", {"reason": [None, True], "meter": "tokens"}, {}),
]:
    node = Node.make(kind=kind, parent=root.id, payload=payload, meta=meta,
                     result={"private": "response"} if kind in {"model_call", "tool_call"} else None)
    store.put(node)

records = []
for node in store.walk(root.id):
    record = dataclasses.asdict(node)
    record["result_text"] = record.pop("_result_text")
    records.append(record)
fixture = {"provenance": {"wheel_sha256": sha}, "root_id": root.id,
           "root_label": _label(root), "records": records,
           "public": tree_document(store, root.id),
           "payloads": tree_document(store, root.id, include_payloads=True),
           "ascii": render_ascii(store, root.id),
           "unicode": render_ascii(store, root.id, unicode=True)}
Path(__file__).with_name("pypi160_cli.json").write_text(
    json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Generated {len(records)} CLI nodes and four output comparisons")
