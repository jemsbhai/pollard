import json
import sys
import time
import warnings
from pollard import MemoryStore, Runtime
import pollard
assert pollard.__version__ == "1.6.0"
warnings.simplefilter("ignore")
n = int(sys.argv[1])
assert n > 0
run = Runtime(MemoryStore()).run("stream-memory")
seen = 0
def on_delta(_):
    global seen
    seen += 1
started = time.perf_counter()
node = run.model_call({"bench": "stream-memory"},
                      fn=lambda _: ({"result": {"text": "ok"}} for _ in range(n)),
                      keep_chunks=False, on_delta=on_delta)
seconds = time.perf_counter() - started
assert node.result == {"text": "ok"}
assert seen == n and run.report()["spent"]["steps"] == 1
print(json.dumps({"chunks": n, "seconds": seconds, "node_id": node.id,
                  "result_digest": node.result_digest, "callbacks": seen,
                  "pollard_version": pollard.__version__, "pollard_file": pollard.__file__}), flush=True)
time.sleep(0.1)  # Sampling window only; outside the measured operation.
