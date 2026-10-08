"""Generate wire fixtures from the verified Pollard 1.6.0 PyPI wheel (no broker)."""
import hashlib
import json
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
from pollard.stores.kafka import _event, _json_bytes, _node_record, _parse_event, _record_digest
from pollard.tree import Node
assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__

root = Node.make(kind="root", parent=None, attempt=0, payload={"run": "Kafka 雪"}, meta={"budget": {"usd": 0.125}})
child = Node.make(kind="model_call", parent=root.id, attempt=0,
    payload={"model": "mock", "big": 10000000000000000000000000000},
    result={"text": "héllo 雪", "temperature": 1.25, "negative_zero": -0.0,
     "usage": {"input_tokens": 12, "output_tokens": 3}},
    meta={"state": "completed", "charges": {"usd": 0.000125, "tokens": 15}})
rows = []
for store_id, operation, body in [
    ("default", "put", _node_record(root)),
    ("store 雪", "put", _node_record(child)),
    ("default", "meta", {"id": child.id, "patch": {"nested": {"b": True, "a": None}, "tiny": 1e-7}}),
]:
    event, operation_id = _event(store_id, operation, body)
    raw = _json_bytes(event)
    assert _parse_event(raw, offset=0, store_id=store_id) == event
    rows.append({"store_id": store_id, "operation": operation, "body": body,
                 "operation_id": operation_id, "event_utf8": raw.decode(),
                 "record_digest": _record_digest(store_id.encode(), raw)})
output = {"provenance": {"pollard_version": "1.6.0", "pollard_wheel_sha256": sha}, "events": rows}
path = Path(__file__).with_name("pypi160_kafka.json")
path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(path)
