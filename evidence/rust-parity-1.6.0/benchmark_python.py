"""PyPI-only timing worker. Startup, setup and validation are outside timings."""
import json
import hashlib
import sys
import time
import warnings

import pollard
from pollard import MemoryStore, Node, NodeKind, Runtime
from pollard.hashing import node_id

assert pollard.__version__ == "1.6.0", pollard.__version__
warnings.filterwarnings("ignore", message="pollard token meter")


def payload(i):
    return {"model": "offline", "prompt": "Summarize this local deterministic benchmark.", "i": i}


def response(_):
    return {"text": "ok", "usage": {"input_tokens": 2, "output_tokens": 3}}


def populate(n):
    rt = Runtime(MemoryStore())
    run = rt.run("bench")
    for i in range(n):
        run.model_call(payload(i), fn=response)
    return rt, run


def sample(operation, n):
    if operation == "identity":
        p = payload(0)
        started = time.perf_counter()
        for _ in range(n):
            digest = node_id(kind="model_call", parent_id=None, attempt=0, payload=p)
        return time.perf_counter() - started, digest
    if operation == "record":
        rt = Runtime(MemoryStore())
        run = rt.run("bench")
        started = time.perf_counter()
        for i in range(n):
            run.model_call(payload(i), fn=response)
        elapsed = time.perf_counter() - started
        spent = run.report()["spent"]
        assert spent["steps"] == n and spent["tokens"] == 5 * n
        return elapsed, run.cursor_id
    if operation in ("replay", "hybrid"):
        rt, _ = populate(n)
        run = Runtime(rt.store, mode=operation).run("bench")

        def forbidden(_):
            raise AssertionError("replay invoked the provider")

        started = time.perf_counter()
        for i in range(n):
            run.model_call(payload(i), fn=forbidden)
        elapsed = time.perf_counter() - started
        spent = run.report()["spent"]
        assert spent["steps"] == n and spent["tokens"] == 5 * n
        return elapsed, run.cursor_id
    if operation == "walk":
        store = MemoryStore()
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "wide"})
        store.put(root)
        for i in range(n):
            store.put(Node.make(kind=NodeKind.NOTE, parent=root.id, payload={"i": i}))
        started = time.perf_counter()
        nodes = list(store.walk(root.id))
        elapsed = time.perf_counter() - started
        assert len(nodes) == n + 1 and nodes[0].id == root.id
        assert len({node.id for node in nodes}) == n + 1
        assert all(node.parent == root.id for node in nodes[1:])
        return elapsed, hashlib.sha256(b"pollard/v1:result\n" +
                                       "\n".join(node.id for node in nodes).encode("ascii")).hexdigest()
    raise ValueError(operation)


if __name__ == "__main__":
    operation, n, repeats = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    assert n > 0 and repeats > 0
    for _ in range(2):
        sample(operation, n)
    samples = []
    checksums = []
    for _ in range(repeats):
        elapsed, checksum = sample(operation, n)
        samples.append(elapsed)
        checksums.append(checksum)
    print(json.dumps({"operation": operation, "size": n, "samples_seconds": samples,
                      "checksum": checksum, "checksums": checksums, "warmups": 2,
                      "pollard_version": pollard.__version__, "pollard_file": pollard.__file__}))
