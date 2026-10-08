"""Generate replay-contract and comparator oracles from the PyPI 1.6.0 wheel."""
import hashlib
import json
from pathlib import Path
import random
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
from pollard.revalidation import ReplayContract, ExactResultComparator, NormalizedModelComparator, make_revalidation_payload, extract_replay_contract
from pollard.tree import Node
assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__
fixtures = {"provenance": {"version": "1.6.0", "wheel_sha256": sha, "generator": Path(__file__).name}, "comparisons": [], "contracts": []}
def compare(recorded, live):
    fixtures["comparisons"].append({"recorded": recorded, "live": live,
        "exact": ExactResultComparator().compare(recorded, live).to_dict(),
        "normalized": NormalizedModelComparator().compare(recorded, live).to_dict()})
for left, right in [({}, {}), ({"text":"hello"}, {"text":"world"}),
    ({"text":"same","usage":{"input_tokens":1},"id":"old"},{"text":"same","usage":{"input_tokens":2},"id":"new"}),
    ({"custom":1,"usage":1,"provider_usage":1,"chunks":[]},{"custom":1,"usage":2,"provider_usage":2,"chunks":[1]}),
    ({"custom":1},{"custom":True}), ({"custom":1},{"custom":1.0}),
    ({"custom":[1,2]},{"custom":[1,2,3]}), ({"a/b":{"~x":[1]}},{"a/b":{"~x":[2]}}),
    ({"text":"same"},{"text":"same","refusal":None}),
    ({"text":"same","structured_output":{"x":1}},{"text":"same","structured_output":{"x":2}}),
    ({"tool_calls":[{"id":"a","function":{"name":"f","arguments":'{"b":2,"a":1}'}}]}, {"tool_calls":[{"id":"b","function":{"name":"f","arguments":'{ "a": 1, "b": 2 }'}}]}),
    ({"tool_calls":[{"call_id":"a","arguments":"bad json"}]},{"tool_calls":[{"call_id":"b","arguments":"bad json"}]}),
    ({"tool_calls":[{"toolUseId":"a","index":0,"input":{"x":1}}]},{"tool_calls":[{"toolUseId":"b","index":1,"input":{"x":1}}]}),
    ({"tool_calls":[{"input_json":"[1,2]"}]},{"tool_calls":[{"input_json":[1,2]}]}),
    ({"tool_calls":[1,None,"x"]},{"tool_calls":[1,None,"y"]}),
    ({"x":list(range(100))},{"x":[-1]*100}), ({"x":list(range(101))},{"x":[-1]*101}),
    ({str(i):0 for i in range(102)},{str(i):1 for i in range(102)}),
]: compare(left, right)
rng = random.Random(160)
atoms = [None, True, False, 0, 1, 1.0, 1.5, 2**128, "x", "é", [], {}]
for _ in range(80):
    compare({"structured_output": [rng.choice(atoms) for _ in range(rng.randrange(1,8))]},
            {"structured_output": [rng.choice(atoms) for _ in range(rng.randrange(1,8))]})
recorded = Node.make(kind="model_call", parent="a"*64, payload={"model":"fixed"}, result={"text":"recorded"})
for options in [{"provider":"openai"}, {"provider":"custom", "model_revision":"m-1", "api_version":"v1", "adapter":"native", "adapter_version":"1", "sdk":"rust", "sdk_version":"1", "application_revision":"abc", "environment":{"region":"local","huge":2**128,"enabled":True}}]:
    contract = ReplayContract(**options)
    for payload in [{"model":"fixed"}, {"_pollard":None}, {"_pollard":{"custom":"keep"}}, {"_pollard":{"replay_contract":contract.to_dict()}}]:
        bound = contract.bind(payload)
        fixtures["contracts"].append({"options":options,"payload":payload,"contract":contract.to_dict(),"bound":bound,"extracted":extract_replay_contract(bound),
            "revalidation_payload":make_revalidation_payload(payload, observation_id="obs-1", recorded_node_id=recorded.id, recorded_result_digest=recorded.result_digest, contract=contract, comparator_name="normalized-model/v1")})
Path(__file__).with_name("pypi160_revalidation.json").write_text(json.dumps(fixtures, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
print(f"Generated {len(fixtures['comparisons'])} comparisons and {len(fixtures['contracts'])} contracts")
