"""Verify the public Rust archive and build a fresh registry-only consumer.

Run after publishing; this script never uploads, logs in, or reads credentials.
The first consumer build resolves without the repository's dependency lockfile.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[2]

SOURCE = r'''
use pollardai::*;
fn main() -> Result<()> {
    let path = std::env::current_dir().unwrap().join("consumer.db");
    let runtime = Runtime::new(SQLiteStore::open(&path)?, ReplayMode::Record);
    let mut run = runtime.run("first-run", Some(Budget {
        tokens: Some(10), steps: Some(1), ..Default::default()
    }), 0)?;
    let root = run.root_id().to_owned();
    assert_eq!(root, "fb4f2a23cc196e53f0fa800a71c025e0a9b7ac5890b83c4d9d1a0214175d9dd5");
    let payload = json!({"model":"local-demo", "prompt":"hello"});
    let recorded = run.model_call(payload.clone(), CallOptions::default(), |_| {
        Ok(json!({"text":"offline reply for hello", "usage":{
            "input_tokens":2, "output_tokens":4
        }}))
    })?;
    assert_eq!(recorded.id, "c4882b75addd9867f623049798e2c6cebc3d49daa80bd5a825c102cf0580fd30");
    assert_eq!(run.spent()?.tokens, 6);
    drop(run);
    drop(runtime);
    let replay = Runtime::new(SQLiteStore::open_read_only(&path)?, ReplayMode::Replay);
    let mut cached = replay.run("first-run", None, 0)?;
    assert_eq!(cached.model_call(payload, CallOptions::default(), |_| {
        panic!("strict replay cannot dispatch")
    })?.id, recorded.id);
    let store = SQLiteStore::open_read_only(&path)?;
    assert_eq!(seal(&store, &root)?.entries.len(), 2);
    assert_eq!(parse_decimal_exact("1.25e-3").unwrap().to_string(), "0.00125");
    assert!(parse_decimal_exact("1e-29").is_err());
    println!("registry consumer: Python golden identities, SQLite persistence, budgets, readonly replay, seal and exact decimals passed");
    Ok(())
}
'''


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pollard-native-release-verification/0.2"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    base = "https://crates.io/api/v1/crates/pollardai/" + args.version
    metadata = json.loads(get(base))["version"]
    archive = get("https://static.crates.io/crates/pollardai/pollardai-" + args.version + ".crate")
    digest = hashlib.sha256(archive).hexdigest()
    assert metadata["num"] == args.version and not metadata["yanked"]
    assert digest == metadata["checksum"] == args.expected_sha256
    (output / f"pollardai-{args.version}.crate").write_bytes(archive)
    consumer = output / "consumer"
    (consumer / "src").mkdir(parents=True)
    (consumer / "Cargo.toml").write_text(
        '[package]\nname="pollard-public-consumer"\nversion="0.0.0"\nedition="2021"\n'
        '[dependencies]\npollardai="=' + args.version + '"\n', encoding="utf-8")
    (consumer / "src/main.rs").write_text(SOURCE, encoding="utf-8")
    checks = []
    for toolchain in ["+1.74.0", "+stable"]:
        rustc_version = subprocess.check_output(
            ["rustc", toolchain, "--version"], text=True).strip()
        cargo_version = subprocess.check_output(
            ["cargo", toolchain, "--version"], text=True).strip()
        command = ["cargo", toolchain, "run"]
        if toolchain == "+stable":
            command += ["--locked"]
        # Each run gets its own empty working directory and database.
        work = consumer / ("run-" + toolchain.lstrip("+"))
        work.mkdir()
        command += ["--manifest-path", str(consumer / "Cargo.toml")]
        result = subprocess.run(command, cwd=work, capture_output=True, text=True)
        log = output / ("consumer-" + toolchain.lstrip("+") + ".log")
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        checks.append({"command": command, "exit_code": result.returncode,
                       "rustc": rustc_version, "cargo": cargo_version,
                       "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()})
        if result.returncode:
            raise RuntimeError(log.read_text(encoding="utf-8"))
    lock = (consumer / "Cargo.lock").read_text(encoding="utf-8")
    package = lock.split('name = "pollardai"', 1)[1].split("[[package]]", 1)[0]
    assert 'source = "registry+' in package and digest in package
    receipt = {"verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "version": args.version, "registry_metadata": metadata,
               "archive_sha256": digest, "archive_bytes": len(archive),
               "consumer_source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
               "consumer_lock_sha256": hashlib.sha256(lock.encode()).hexdigest(),
               "registry_only": True, "checks": checks, "passed": True}
    (output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"version": args.version, "sha256": digest, "passed": True}))


if __name__ == "__main__":
    main()
