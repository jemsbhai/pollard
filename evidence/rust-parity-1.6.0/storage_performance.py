"""SQLite release-build comparison. No budgets/network; default WAL/NORMAL in both."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
from benchmark import ROOT,WHEEL_HASH,wheel_root,provenance,source_fingerprints,fingerprint,validate_python_origin,output

if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("storage-performance.json"))
    args = parser.parse_args()
    package=wheel_root()
    env=dict(os.environ,PYTHONPATH=str(package),PYTHONHASHSEED="0")
    native=ROOT/"crates/pollardai/target/release/examples"/("storage_performance.exe" if os.name=="nt" else "storage_performance")
    evidence=provenance(package,[native])
    rows=[];rng=random.Random(160)
    for operation in ["record","hybrid","replay"]:
        commands=[("python-1.6.0",[sys.executable,str(Path(__file__).with_name("storage_performance_python.py"))]),("rust-updated",[str(native)])]
        rng.shuffle(commands);engines={}
        for name,command in commands:
            print(name,operation,40,flush=True)
            data=json.loads(output(command+[operation,"40","7"],env=env))
            assert data["operation"]==operation and data["size"]==40
            assert len(data["samples_seconds"])==7 and all(t>0 for t in data["samples_seconds"])
            assert len(data["checksums"])==7
            if name=="python-1.6.0":validate_python_origin(data,package)
            engines[name]=dict(data,median_seconds=statistics.median(data["samples_seconds"]),min_seconds=min(data["samples_seconds"]),max_seconds=max(data["samples_seconds"]))
        assert len({c for data in engines.values() for c in data["checksums"]})==1
        rows.append({"operation":operation,"size":40,"engines":engines,"speedup_vs_python":engines["python-1.6.0"]["median_seconds"]/engines["rust-updated"]["median_seconds"]})
    assert source_fingerprints()==evidence["source_sha256"],"sources changed during measurement"
    assert fingerprint([native])==evidence["binary_sha256"]
    result={"generated_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),**evidence,
        "source_unchanged_during_run":True,"wheel_sha256":WHEEL_HASH,"platform":platform.platform(),"python":sys.version,
        "method":{"warmups":2,"samples":7,"engine_order_seed":160,"fresh_database_per_sample":True,"journal_mode":"WAL","synchronous":"NORMAL","budgets":False,"readonly_replay":True,"setup_and_startup_timed":False,"provider_network":False},"results":rows}
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([{k:v for k,v in row.items() if k!="engines"} for row in rows],indent=2))
