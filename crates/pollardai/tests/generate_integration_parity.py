"""SHA-pinned Python MCP identity and content-free telemetry fixtures."""
import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
import sys

wheel=Path(sys.argv[1]).resolve()
sha="569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sha
sys.path.insert(0,str(wheel))
import pollard
from pollard.mcp import registry_from_mcp
from pollard.otel import span_attributes, _span_name
from pollard.tree import Node
assert pollard.__version__=="1.6.0" and str(wheel) in pollard.__file__
class Session:
    def __init__(self,listing): self.listing=listing
    async def list_tools(self): return self.listing
    async def call_tool(self,*args): raise AssertionError("discovery cannot dispatch")
fixtures={"provenance":{"wheel_sha256":sha},"mcp":[],"otel":[]}
for listing,exclude in [({},[]),({"tools":[]},[]),({"tools":[{"name":"echo","description":"Echo text","inputSchema":{"type":"object","properties":{"text":{"type":"string","sensitive":True}},"required":["text"],"additionalProperties":False}}]},[]),
    ({"tools":[{"name":"excluded","inputSchema":None},{"name":"blank","description":9}]},["excluded"]),
    ({"tools":[{"name":"refs","input_schema":{"type":"object","properties":{"n":{"$ref":"#/$defs/n"}},"$defs":{"n":{"type":"integer"}}}}]},[]),
    ({"tools":None},[]),({"tools":[{}]},[]),({"tools":[{"name":"x","inputSchema":[]} ]},[]),
    ({"tools":[{"name":"x","inputSchema":{"allOf":[]}}]},[]),({"tools":[{"name":"x"},{"name":"x"}]},[])]:
    case={"listing":listing,"exclude":exclude}
    try:
        registry=asyncio.run(registry_from_mcp(Session(listing),exclude=set(exclude)))
        case["digest"]=registry.registry_digest
    except Exception as error: case["error"]=type(error).__name__
    fixtures["mcp"].append(case)
for payload in [{"model":"openai/gpt-demo"},{"modelId":"bedrock/model"},{"model":"azure/a"},{"model":"vertex_ai/a"},{"model":"gemini/a"},{"model":"anthropic/a"},{"model":"unknown","_pollard":{"provider":"custom"}},{"model":None},{"model":False},{}]:
    for meta in [{},{"usage":{"input_tokens":True,"output_tokens":2},"pruned":1},{"registry_digest":"meta","charges":{"usd":0.1,"tokens":3,"bool":True,"string":"private"},"avoided":{"tokens":4},"pruned":True}]:
        node=Node.make(kind="model_call",parent="a"*64,payload={**payload,"prompt":"PRIVATE_INPUT"},
            result={"text":"PRIVATE_OUTPUT","model":"returned","usage":{"input_tokens":5,"output_tokens":8}},meta=meta)
        record=dataclasses.asdict(node); record["result_text"]=record.pop("_result_text")
        fixtures["otel"].append({"node":record,"attributes":span_attributes(node),"name":_span_name(node)})
for kind,payload in [("root",{"run":"private run"}),("tool_call",{"tool":"echo","arguments":{"secret":"private"}}),("refusal",{"reason":"budget","meter":"tokens"}),("note",{})]:
    node=Node.make(kind=kind,parent=None if kind=="root" else "a"*64,payload=payload)
    record=dataclasses.asdict(node); record["result_text"]=record.pop("_result_text")
    fixtures["otel"].append({"node":record,"attributes":span_attributes(node),"name":_span_name(node)})
Path(__file__).with_name("pypi160_integrations.json").write_text(json.dumps(fixtures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(f"Generated {len(fixtures['mcp'])} MCP cases and {len(fixtures['otel'])} telemetry cases")
