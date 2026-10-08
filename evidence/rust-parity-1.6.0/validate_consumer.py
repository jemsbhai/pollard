"""Compile and run a new consumer without borrowing the library's Cargo.lock.

Use an output directory that does not exist. Resolution deliberately runs online
with the selected Cargo version: a locked library build cannot validate the
dependency versions that a newly installed downstream application will receive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import tomllib

SOURCE = r"""use pollardai::{json, Budget, CallOptions, ReplayMode, Result, Runtime};

fn main() -> Result<()> {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("consumer", Some(Budget {
        tokens: Some(10), steps: Some(1), ..Default::default()
    }), 0)?;
    let payload = json!({"model":"consumer", "prompt":"hello"});
    let recorded = run.model_call(payload.clone(), CallOptions::default(), |_| {
        Ok(json!({"text":"offline reply", "usage":{
            "input_tokens":2, "output_tokens":4
        }}))
    })?;
    assert_eq!(run.spent()?.tokens, 6);
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut cached = replay.run("consumer", None, 0)?;
    assert_eq!(cached.model_call(payload, CallOptions::default(), |_| {
        panic!("strict replay cannot dispatch")
    })?.id, recorded.id);
    println!("recorded_tokens=6 strict_replay=true");
    Ok(())
}
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crate", type=Path, default=Path("crates/pollardai"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--toolchain", help="Rustup toolchain, for example 1.74.0")
    parser.add_argument("--all-features", action="store_true")
    args = parser.parse_args()
    crate, output = args.crate.resolve(), args.output_dir.resolve()
    manifest = crate / "Cargo.toml"
    configuration = tomllib.loads(manifest.read_text(encoding="utf-8"))
    features = (
        sorted(set(configuration.get("features", {})) - {"default"}) if args.all_features else []
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "src").mkdir()
    dependency = {"path": crate.as_posix(), "features": features}
    # JSON strings and arrays are valid TOML basic strings and arrays.
    fields = ", ".join(f"{key} = {json.dumps(value)}" for key, value in dependency.items())
    consumer = output / "Cargo.toml"
    consumer.write_text(
        '[package]\nname = "pollard-fresh-consumer"\nversion = "0.0.0"\nedition = "2021"\n'
        "[workspace]\n\n[dependencies]\npollardai = { " + fields + " }\n",
        encoding="utf-8",
    )
    (output / "src/main.rs").write_text(SOURCE, encoding="utf-8")
    library_lock = crate / "Cargo.lock"
    library_lock_hash = sha256(library_lock) if library_lock.exists() else None
    cargo = ["cargo", *([f"+{args.toolchain}"] if args.toolchain else [])]
    report = {
        "status": "running",
        "crate_version": configuration["package"]["version"],
        "features": features,
        "toolchain": args.toolchain or "default",
        "consumer_started_without_lockfile": not (output / "Cargo.lock").exists(),
        "library_lock_sha256_before": library_lock_hash,
        "manifest_sha256": sha256(manifest),
        "source_sha256": {
            p.relative_to(crate).as_posix(): sha256(p)
            for p in sorted((crate / "src").rglob("*.rs"))
        },
        "commands": [],
    }
    environment = {**os.environ, "CARGO_TERM_COLOR": "never"}
    try:
        for name, command in [
            ("toolchain", [*cargo, "--version"]),
            ("resolve", [*cargo, "generate-lockfile", "--manifest-path", str(consumer)]),
            ("run", [*cargo, "run", "--locked", "--manifest-path", str(consumer)]),
        ]:
            print(f"Fresh consumer: {name} ({','.join(features) or 'default'})", flush=True)
            started = time.monotonic()
            completed = subprocess.run(
                command,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            (output / f"{name}.log").write_text(completed.stdout, encoding="utf-8")
            report["commands"].append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": completed.returncode,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            print(completed.stdout, end="", flush=True)
            if completed.returncode:
                raise RuntimeError(f"fresh consumer {name} failed; see {output / (name + '.log')}")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        report["library_lock_sha256_after"] = (
            sha256(library_lock) if library_lock.exists() else None
        )
        if report["library_lock_sha256_after"] != library_lock_hash:
            report["status"] = "failed"
            report["error"] = "library lockfile changed during downstream validation"
        lock = output / "Cargo.lock"
        if lock.exists():
            report["consumer_lock_sha256"] = sha256(lock)
            report["resolved_packages"] = [
                {"name": package["name"], "version": package["version"]}
                for package in tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
            ]
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
