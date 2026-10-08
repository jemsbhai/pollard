"""Run the final native matrix and preserve exact commands, logs and source hashes.

Live databases are validated separately by validate_remote.py. Hardware NVML is
explicitly opt-in; test skips are reported, never treated as live coverage.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MANIFEST = "crates/pollardai/Cargo.toml"
OPTIONAL = "postgres,redis-tls,mongodb,neo4j,kafka,nvml,estimate-openai"


def source_hashes():
    crate = ROOT / "crates/pollardai"
    files = [crate / "Cargo.toml", crate / "Cargo.lock"]
    for directory in ("src", "examples", "tests"):
        files.extend(p for p in (crate / directory).rglob("*")
                     if p.is_file() and p.suffix in (".rs", ".py", ".json"))
    files += list(HERE.glob("*.py"))
    files += [ROOT / ".github/workflows/native.yml"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nvml", action="store_true")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=HERE / "final-validation")
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    before = source_hashes()
    run_id = uuid.uuid4().hex[:12]
    checks = []
    result = {"started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "platform": platform.platform(), "python": sys.version,
              "git_base": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": before, "checks": checks,
              "scope": "Native local validation; separate live-service runner and TLS Linux log. Ignored tests are excluded from pass counts."}
    commands = [
        ("format", ["cargo", "fmt", "--manifest-path", MANIFEST, "--check"]),
        ("format-msrv", ["cargo", "+1.74.0", "fmt", "--manifest-path", MANIFEST, "--check"]),
        ("clippy-default", ["cargo", "clippy", "--manifest-path", MANIFEST, "--all-targets", "--locked", "--", "-D", "warnings"]),
        ("clippy-msrv", ["cargo", "+1.74.0", "clippy", "--manifest-path", MANIFEST, "--all-targets", "--locked", "--", "-D", "warnings"]),
        ("clippy-optional", ["cargo", "clippy", "--manifest-path", MANIFEST, "--all-targets", "--locked", "--features", OPTIONAL, "--", "-D", "warnings"]),
        ("clippy-optional-msrv", ["cargo", "+1.74.0", "clippy", "--manifest-path", MANIFEST, "--all-targets", "--locked", "--features", OPTIONAL, "--", "-D", "warnings"]),
        ("test-stable", ["cargo", "test", "--manifest-path", MANIFEST, "--locked"]),
        ("test-msrv", ["cargo", "+1.74.0", "test", "--manifest-path", MANIFEST, "--locked"]),
        ("test-release", ["cargo", "test", "--release", "--manifest-path", MANIFEST, "--locked"]),
        ("test-optional", ["cargo", "test", "--manifest-path", MANIFEST, "--locked", "--features", OPTIONAL]),
        ("test-optional-msrv", ["cargo", "+1.74.0", "test", "--manifest-path", MANIFEST, "--locked", "--features", OPTIONAL]),
        ("package", ["cargo", "package", "--manifest-path", MANIFEST, "--locked", "--allow-dirty"]),
    ]
    for name in ("sqlite", "arbitration", "runtime"):
        commands.append((f"interop-{name}", [sys.executable, str(HERE / f"{name}_interop.py"),
                        "--wheel", str(wheel), "--crate", "crates/pollardai",
                        "--output-dir", f".benchmarks/final-{name}-interop-{run_id}"]))
    if args.nvml:
        commands.append(("nvml-hardware", ["cargo", "test", "--manifest-path", MANIFEST, "--locked",
                        "--features", "nvml", "--test", "nvml", "--", "--ignored", "--nocapture"]))
    for name, command in commands:
        print(f"RUN {name}", flush=True)
        started = time.perf_counter()
        log = destination / f"{name}.log"
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
        text = log.read_text(encoding="utf-8", errors="replace")
        summaries = re.findall(r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored", text)
        checks.append({"name": name, "command": command, "exit_code": completed.returncode,
                       "seconds": time.perf_counter()-started, "log": log.name,
                       "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                       "test_summary": {"passed": sum(int(s[1]) for s in summaries),
                                        "failed": sum(int(s[2]) for s in summaries),
                                        "ignored": sum(int(s[3]) for s in summaries)} if summaries else None})
        result.update(finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      source_unchanged_during_run=source_hashes() == before,
                      all_checks_passed=all(c["exit_code"] == 0 for c in checks) and len(checks) == len(commands))
        (destination / "validation.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
        print(f"{name}: exit {completed.returncode}", flush=True)
        if completed.returncode:
            print(text[-12000:])
            raise SystemExit(completed.returncode)
    assert source_hashes() == before, "source changed during validation; rerun final matrix"
    print(json.dumps({c["name"]: c["test_summary"] or "passed" for c in checks}, indent=2))


if __name__ == "__main__":
    main()
