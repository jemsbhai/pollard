"""Generate OpenAI textual-leaf estimator fixtures from pinned Pollard and tiktoken."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
wheel=Path(sys.argv[1]).resolve()
sha="569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sha
sys.path.insert(0,str(wheel))
import pollard
import tiktoken
from pollard.estimators.openai import OpenAITokenEstimator,_fallback_encoding_name
assert pollard.__version__=="1.6.0" and str(wheel) in pollard.__file__
fixtures={"provenance":{"wheel_sha256":sha,"tiktoken_version":importlib.metadata.version("tiktoken")},"cases":[],"encodings":{}}
for name in ["cl100k_base","o200k_base","p50k_base","p50k_edit","r50k_base"]:
    encoding=tiktoken.get_encoding(name)
    digest=hashlib.sha256()
    for token,rank in sorted(encoding._mergeable_ranks.items()): digest.update(len(token).to_bytes(8,"big")+token+rank.to_bytes(8,"big"))
    fixtures["encodings"][name]={"vocab_sha256":digest.hexdigest(),"special_tokens":encoding._special_tokens}
for model in [None,"","unknown","gpt-4o","gpt-5-demo","gpt-4.1-mini","gpt-4.5-preview","chatgpt-4o-latest","o1-demo","o3-demo","o4-mini","gpt-3.5-turbo","text-davinci-003","text-davinci-edit-001","davinci","namespace:GPT-5-custom"]:
    for payload in [{"prompt":"Hello, world!"},{"model":model,"messages":[{"role":"user","content":"Hello \u00e9 \U0001f98a\n\u4e16\u754c"},{"role":"assistant","content":[{"text":"fine","n":3}]}],"nested":{"model":"skip me","array":["model",True,None,3]}}, {"model":model,"prompt":"<|endoftext|>"}]:
        for forced in [None,"","gpt-4o"]:
            row={"model":forced,"tokens_per_message":3,"payload":payload,"fallback":_fallback_encoding_name(forced or payload.get("model"))}
            try: row["tokens"]=OpenAITokenEstimator(forced).estimate_input_tokens(payload)
            except Exception as error: row["error"]=type(error).__name__
            fixtures["cases"].append(row)
Path(__file__).with_name("pypi160_estimators.json").write_text(json.dumps(fixtures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(f"Generated {len(fixtures['cases'])} input estimator cases")
