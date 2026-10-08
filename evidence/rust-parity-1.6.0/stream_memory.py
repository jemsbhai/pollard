"""External process memory comparison; `pip install psutil` is required.

Reports peak working set on Windows, sampled RSS elsewhere. This includes
interpreter/library startup memory. It is not an allocator-only measurement.
"""
import argparse
import json
import datetime
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time
import psutil
from benchmark import ROOT, WHEEL_HASH, wheel_root, provenance, source_fingerprints, fingerprint, validate_python_origin


def monitor(command, env):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env, cwd=ROOT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    handle = psutil.Process(process.pid)
    peak = 0
    metric = "sampled_rss"
    while process.poll() is None:
        try:
            memory = handle.memory_info()
            if hasattr(memory, "peak_wset"):
                metric = "peak_working_set"
                peak = max(peak, memory.peak_wset)
            else:
                peak = max(peak, memory.rss)
        except psutil.NoSuchProcess:
            break
        time.sleep(0.005)
    stdout, stderr = process.communicate()
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert peak > 0, "process exited before any memory measurement"
    return dict(result, peak_memory_bytes=peak, memory_metric=metric)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("stream-memory.json"))
    args = parser.parse_args()
    package = wheel_root()
    env = dict(os.environ, PYTHONPATH=str(package), PYTHONHASHSEED="0")
    native = ROOT / "crates/pollardai/target/release/examples" / ("stream_memory.exe" if os.name == "nt" else "stream_memory")
    evidence = provenance(package, [native])
    rows = []
    rng = random.Random(160)
    for chunks in [10000, 200000]:
        engines = {}
        commands = [("python-1.6.0", [sys.executable, str(Path(__file__).with_name("stream_memory_python.py"))]),
                    ("rust-updated", [str(native)])]
        rng.shuffle(commands)
        for name, command in commands:
            print(name, chunks, flush=True)
            samples = [monitor(command + [str(chunks)], env) for _ in range(3)]
            for sample in samples:
                assert sample["chunks"] == chunks and sample["callbacks"] == chunks
                assert sample["seconds"] > 0
                if name == "python-1.6.0":
                    validate_python_origin(sample, package)
            engines[name] = {"samples": samples,
                             "median_seconds": statistics.median(s["seconds"] for s in samples),
                             "median_peak_bytes": statistics.median(s["peak_memory_bytes"] for s in samples)}
        assert len({(s["node_id"], s["result_digest"]) for e in engines.values() for s in e["samples"]}) == 1
        python, rust = engines["python-1.6.0"], engines["rust-updated"]
        rows.append({"chunks": chunks, "engines": engines,
                     "peak_memory_reduction_percent": 100 * (1 - rust["median_peak_bytes"] / python["median_peak_bytes"]),
                     "speedup": python["median_seconds"] / rust["median_seconds"]})
    assert source_fingerprints() == evidence["source_sha256"], "sources changed while measuring memory"
    assert fingerprint([native]) == evidence["binary_sha256"], "binary changed while measuring memory"
    document = {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                **evidence, "source_unchanged_during_run": True,
                "platform": platform.platform(), "python": sys.version, "wheel_sha256": WHEEL_HASH,
                "keep_chunks": False, "constant_final_result": True,
                "memory_includes_startup": True, "poll_interval_seconds": 0.005,
                "samples_per_case": 3, "engine_order_seed": 160,
                "memory_method_note": "Total process peak includes startup and runtime. Windows peak working set is OS-reported; non-Windows RSS is sampled every 5 ms and may miss transient peaks. The 100 ms final sleep is outside operation timing.",
                "results": rows}
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{k: v for k, v in row.items() if k != "engines"} for row in rows], indent=2))
