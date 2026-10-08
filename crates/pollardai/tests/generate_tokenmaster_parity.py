"""Generate native governance fixtures from Pollard 1.6.0 + Tokenmaster 0.2.0.

Usage: python generate_tokenmaster_parity.py <pollard-1.6.0-py3-none-any.whl>
Tokenmaster's installed source and catalog are SHA-pinned below; no network used.
The imported catalog is test evidence, not a live price recommendation.
Tokenmaster source/catalog: MIT, Copyright (c) 2026 Muntaser Syed.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import warnings

wheel = Path(sys.argv[1]).resolve()
wheel_sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == wheel_sha
sys.path.insert(0, str(wheel))
import pollard
import tokenmaster
from pollard.meters.tokenmaster import TokenmasterMeter, TokenmasterCostMeter, _exclusive_turn_payload
from pollard.meters import MeterPrecheckRefusal
from tokenmaster import ModelProfile, TurnUsage
from tokenmaster.registry import default_registry

assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__
assert importlib.metadata.version("tokenmaster") == "0.2.0"
tmroot = Path(tokenmaster.__file__).parent
hashes = {
    "types.py": "fa5fb15ec5c300a8d71448a1507d1b941c4d71154c1b46fd598efd5aedb85470",
    "registry.py": "8afa74dd1609b7d06279d41da802761065b764b785c9836d33bcccbe01025da2",
    "meter.py": "3e25155660c83432dd3b10a34fceb1ead9f7320737c55d0dad6253c9ad31943e",
    "advisor.py": "0942e194f530a618afd22ca278d01b67733ed87d08d871d467aefaad150dfaa8",
    "data/models.json": "43ba8f4f7db0079d60476b4ae62299fa4423dc835fc2e45d5fcba7e283e065e4",
}
for path, sha in hashes.items():
    assert hashlib.sha256((tmroot / path).read_bytes()).hexdigest() == sha, path
catalog = json.loads((tmroot / "data/models.json").read_text("utf-8"))
custom = [
    {"model_id":"test:effective","provider":"test","window_nominal":1000,"max_output":100,"effective":{"model_id":"test:effective","effective_context":800,"method":"test","source":"fixture"},"pricing":{"input":10,"cache_read":1,"cache_write":12,"output":20}},
    {"model_id":"test:no-output","provider":"test","window_nominal":1000},
    {"model_id":"test:eur","provider":"test","window_nominal":1000,"pricing":{"input":1,"output":2,"currency":"EUR"}},
]
for entry in custom:
    default_registry().register(ModelProfile.from_dict(entry))
catalog["models"].extend(custom)
fixtures = {"provenance":{"pollard_version":"1.6.0","pollard_wheel_sha256":wheel_sha,"tokenmaster_version":"0.2.0","tokenmaster_source_sha256":hashes,"generator":Path(__file__).name},"catalog":catalog,"aliases":[],"limits":[],"quotes":[],"usage":[],"prechecks":[],"sequences":[]}
for model in ["gpt-5.6"," OPENAI:GPT-5.6 ","gpt-5.6-2026-07-31","claude-haiku-4-5-20251001","test:effective"]:
    fixtures["aliases"].append({"model":model,"profile":tokenmaster.get_profile(model).to_dict()})
for model, inputs in [("gpt-5.6",[0,922000,922001,1050001]),("test:effective",[0,700,701,900,901]),("test:no-output",[0,1000,1001])]:
    for capacity in ["nominal","effective"]:
        for count in inputs:
            for requested, reserved in [(None,0),(100,0),(101,50),(128001,0),(0,1001)]:
                check=tokenmaster.check_request_limits(model,input_tokens=count,requested_output_tokens=requested,reserved_output_tokens=reserved,capacity=capacity)
                fixtures["limits"].append({"model":model,"input":count,"requested":requested,"reserved":reserved,"capacity":capacity,"check":check.to_dict()})
for model in ["gpt-5.6","test:effective","test:eur","google:gemini-3.1-pro"]:
    for count in [0,100,272000,272001,300000]:
        for reserved in [0,10000]:
            for conservative in [False,True]:
                row={"model":model,"input":count,"reserved":reserved,"conservative":conservative}
                try: row["quote"]=tokenmaster.quote_estimate(model,input_tokens=count,reserved_output_tokens=reserved,conservative=conservative).to_dict()
                except ValueError: row["error"]=True
                fixtures["quotes"].append(row)
usages=[{}, {"usage":{}}, {"usage":{"input_tokens":3,"output_tokens":2}},
    {"usage":{"prompt_tokens":5,"completion_tokens":4,"cached_input_tokens":3,"cache_write_input_tokens":2,"reasoning_tokens":1}},
    {"usage":{"input_tokens":100,"output_tokens":40},"provider_usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":20,"cache_write_tokens":30},"output_tokens":40,"output_tokens_details":{"reasoning_tokens":10}}},
    {"usage":{"input_tokens":15,"output_tokens":4},"provider_usage":{"input_tokens":10,"cache_read_input_tokens":2,"cache_creation_input_tokens":3,"output_tokens":4}},
    {"usage":{"input_tokens":15,"output_tokens":4},"provider_usage":{"inputTokens":10,"cacheReadInputTokens":2,"cacheWriteInputTokens":3,"outputTokens":4}},
    {"usage":{"input_tokens":10,"output_tokens":4},"provider_usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":20,"cache_write_tokens":30},"output_tokens":4,"output_tokens_details":{"reasoning_tokens":10}}},
    {"usage":{"input_tokens":8,"output_tokens":3},"provider_usage":{"input_tokens":True,"output_tokens":-1,"input_tokens_details":{"cached_tokens":False},"cached_input_tokens":2}},
    {"usage":{"input_tokens":300000,"output_tokens":10000},"provider_usage":{"input_tokens":300000,"input_tokens_details":{"cached_tokens":100000,"cache_write_tokens":50000},"output_tokens":10000,"output_tokens_details":{"reasoning_tokens":2000}}},
]
for result in usages:
    usage=_exclusive_turn_payload(result)
    row={"result":result,"exclusive":usage,"quotes":[]}
    for model in ["gpt-5.6","test:effective","google:gemini-3.1-pro"]:
        quote={"model":model}
        try: quote["quote"]=tokenmaster.quote_usage(model,TurnUsage(turn_id=0,**usage)).to_dict()
        except ValueError: quote["error"]=True
        row["quotes"].append(quote)
    fixtures["usage"].append(row)

class Estimator:
    def __init__(self,value): self.value=value
    def estimate_input_tokens(self,payload): return self.value

def build(config,cost=False):
    kwargs={"model":config.get("model"),"estimator":Estimator(config.get("estimate")),"reserved_output":config.get("reserved",0)}
    if cost: return TokenmasterCostMeter(**kwargs)
    return TokenmasterMeter(**kwargs,enforce_profile_limits=config.get("enforce",False),profile_capacity=config.get("capacity","nominal"),expected_remaining_turns=config.get("turns"))

configs=[{"model":"gpt-5.6","estimate":922000,"enforce":True}, {"model":"gpt-5.6","estimate":922001,"enforce":True}, {"model":"test:effective","estimate":701,"enforce":True,"capacity":"effective"}, {"model":"gpt-5.6","estimate":None,"enforce":True}, {"estimate":1,"enforce":True}, {"model":"gpt-5.6","estimate":300000,"reserved":10000}, {"model":"test:no-output","estimate":1}, {"model":"test:eur","estimate":1}, {"model":"google:gemini-3.1-pro","estimate":1}]
for config in configs:
    for cost in [False,True]:
        for payload in [{},{"max_tokens":128001},{"max_output_tokens":100,"max_tokens":True},{"max_completion_tokens":-1}]:
            row={"config":config,"cost":cost,"payload":payload}
            try:
                value=build(config,cost).precheck_estimate("model_call",payload)
                row["estimate"]=None if value is None else str(value)
            except MeterPrecheckRefusal as exc:
                row["refusal"]={"reason":exc.reason,"audit_meta":exc.audit_meta,"requested":exc.requested,"remaining":exc.remaining}
            except ValueError: row["invalid"]=True
            fixtures["prechecks"].append(row)

sequences=[({"model":"test:effective","estimate":1,"reserved":50,"enforce":True,"capacity":"effective","turns":5}, [
    {"usage":{"input_tokens":100,"output_tokens":20}},
    usages[4],{"usage":{"input_tokens":520,"output_tokens":40}},
    {"usage":{"input_tokens":640,"output_tokens":40}},
    {"usage":{"input_tokens":760,"output_tokens":50}},
    {"usage":{"input_tokens":10,"output_tokens":10}}]),
    ({"estimate":10,"reserved":2},[{"model":"gpt-5.6","usage":{"input_tokens":10,"output_tokens":2}},{"model":"gpt-5.5","usage":{"input_tokens":20,"output_tokens":4}},{"model":"gpt-5.6","usage":{"input_tokens":30,"output_tokens":6}}]),
    ({"model":"gpt-5.6","estimate":10},usages),
    ({"estimate":10},[{"usage":{"input_tokens":3,"output_tokens":2}}])]
for config,results in sequences:
    meter=build(config);cost=build(config,True);rows=[]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for result in results:
            meta={"sentinel":"keep"}; amount=cost.charge("model_call",{},result,meta);count=meter.charge("model_call",{},result,meta)
            if "turn" in meta.get("tokenmaster",{}): meta["tokenmaster"]["turn"].pop("timestamp")
            rows.append({"result":result,"tokens":count,"cost":str(amount),"meta":meta,"fallback":cost.precheck_fallback_reason("model_call",{},result,meta)})
    fixtures["sequences"].append({"config":config,"rows":rows})

path=Path(__file__).with_name("pypi160_tokenmaster020.json")
path.write_text(json.dumps(fixtures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print({key:len(value) for key,value in fixtures.items() if isinstance(value,list)})
