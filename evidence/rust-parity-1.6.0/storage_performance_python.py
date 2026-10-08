"""Pinned-wheel SQLite timing worker; fresh database per sample, setup excluded."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import pollard
from pollard import Runtime, SQLiteStore

assert pollard.__version__=="1.6.0"
def payload(i): return {"model":"offline","prompt":"Local SQLite benchmark","i":i}
def response(_): return {"text":"ok","usage":{"input_tokens":2,"output_tokens":3}}
def sample(operation,n):
    with tempfile.TemporaryDirectory(prefix="pollard-sqlite-benchmark-") as temporary:
        path=Path(temporary)/"recording.db"
        store=SQLiteStore(path)
        if operation!="record":
            setup=Runtime(store).run("sqlite-benchmark")
            for i in range(n): setup.model_call(payload(i),fn=response)
            store.close()
            store=SQLiteStore(path,read_only=operation=="replay")
        try:
            run=Runtime(store,mode=operation).run("sqlite-benchmark")
            calls=0
            def callback(value):
                nonlocal calls
                assert operation=="record"
                calls+=1
                return response(value)
            started=time.perf_counter()
            for i in range(n): run.model_call(payload(i),fn=callback)
            seconds=time.perf_counter()-started
            assert calls==(n if operation=="record" else 0)
            report=run.report()
            assert report["spent"]["steps"]==n and report["spent"]["tokens"]==5*n
            nodes=list(store.walk(run.root_id))
            assert len(nodes)==n+1 and len({node.id for node in nodes})==n+1
            for node in nodes: assert node.id==node.expected_id
            checksum=hashlib.sha256(b"pollard/v1:result\n"+"\n".join(f"{node.id}:{node.result_digest or ''}" for node in nodes).encode()).hexdigest()
            return seconds,checksum
        finally: store.close()
if __name__=="__main__":
    operation,n,repeats=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
    for _ in range(2): sample(operation,n)
    results=[sample(operation,n) for _ in range(repeats)]
    print(json.dumps({"operation":operation,"size":n,"samples_seconds":[t for t,_ in results],"checksums":[c for _,c in results],"warmups":2,"sqlite_version":sqlite3.sqlite_version,"pollard_version":pollard.__version__,"pollard_file":pollard.__file__}))
