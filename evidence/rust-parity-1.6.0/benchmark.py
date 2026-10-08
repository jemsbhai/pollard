"""Reproduce native release-build timings against the SHA-pinned PyPI wheel.

Run from the repository root after building both Rust examples (see README).
No provider calls, process startup, compilation, or networking are timed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

WHEEL_HASH = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
ROOT = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "51e3a245641044fc8b3d6a90fc49f3cbfcbf107d"


def output(command, **kwargs):
    return subprocess.check_output(command, text=True, cwd=ROOT, **kwargs).strip()


def fingerprint(paths, relative_to=ROOT):
    return {str(path.relative_to(relative_to)).replace("\\", "/"):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def source_fingerprints():
    crate = ROOT / "crates/pollardai"
    paths = list((crate / "src").rglob("*.rs")) + list((crate / "examples").glob("*.rs"))
    paths += [crate / "Cargo.toml", crate / "Cargo.lock"]
    paths += list(Path(__file__).parent.glob("*.py"))
    return fingerprint(paths)


def provenance(package, binaries, *, include_baseline=False):
    sources = source_fingerprints()
    # Fail closed on stale binaries. Hashes identify exactly what was measured;
    # the build itself is performed by the documented preceding Cargo command.
    for binary in binaries:
        crate = binary.parents[3]
        inputs = list((crate / "src").rglob("*.rs"))
        inputs += [crate / "Cargo.toml", crate / "Cargo.lock", crate / "examples" / (binary.stem + ".rs")]
        assert binary.stat().st_mtime_ns >= max(path.stat().st_mtime_ns for path in inputs), \
            f"rebuild stale release binary: {binary}"
    result = {"git_base": output(["git", "rev-parse", "HEAD"]),
              "git_dirty": bool(output(["git", "status", "--porcelain"])),
              "rustc": output(["rustc", "--version"]),
              "source_sha256": sources, "binary_sha256": fingerprint(binaries),
              "python_package_sha256": fingerprint((package / "pollard").rglob("*.py"), package),
              "python_package_root": str(package.resolve())}
    if include_baseline:
        baseline_crate = ROOT / ".benchmarks/rust-baseline/crates/pollardai"
        paths = list((baseline_crate / "src").rglob("*.rs")) + [baseline_crate / "Cargo.toml"]
        for path in paths:
            git_path = "crates/pollardai/" + path.relative_to(baseline_crate).as_posix()
            expected = subprocess.check_output(["git", "show", f"{BASELINE_COMMIT}:{git_path}"], cwd=ROOT)
            assert path.read_bytes().replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n"), \
                f"baseline differs from {BASELINE_COMMIT}: {git_path}"
        current_harness = ROOT / "crates/pollardai/examples/performance.rs"
        baseline_harness = baseline_crate / "examples/performance.rs"
        assert current_harness.read_bytes() == baseline_harness.read_bytes(), "baseline harness differs"
        result.update(baseline_commit=BASELINE_COMMIT, baseline_source_verified=True,
                      baseline_source_sha256=fingerprint(paths + [baseline_crate / "Cargo.lock", baseline_harness]))
    return result


def validate_python_origin(data, package):
    assert Path(data["pollard_file"]).resolve() == (package / "pollard/__init__.py").resolve(), data


def wheel_root():
    destination = ROOT / ".benchmarks" / "rust-python160"
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / "pollard-1.6.0-py3-none-any.whl"
    if not artifact.is_file():
        with urllib.request.urlopen("https://pypi.org/pypi/pollard/1.6.0/json") as response:
            metadata = json.load(response)
        release = next(f for f in metadata["urls"] if f["filename"] == artifact.name)
        assert release["digests"]["sha256"] == WHEEL_HASH
        with urllib.request.urlopen(release["url"]) as response:
            artifact.write_bytes(response.read())
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == WHEEL_HASH
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(destination / "package")
        expected = {name for name in archive.namelist() if name.startswith("pollard/") and name.endswith(".py")}
        actual = {path.relative_to(destination / "package").as_posix()
                  for path in (destination / "package/pollard").rglob("*.py")}
        assert actual == expected, "extracted wheel has unexpected Python modules"
        assert all((destination / "package" / name).read_bytes() == archive.read(name) for name in expected)
    return destination / "package"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("performance.json"))
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.samples < 3:
        parser.error("at least three samples are required")
    suffix = ".exe" if os.name == "nt" else ""
    binary = ROOT / "crates/pollardai/target/release/examples" / ("performance" + suffix)
    baseline = ROOT / ".benchmarks/rust-baseline/crates/pollardai/target/release/examples" / ("performance" + suffix)
    for executable in [binary, baseline]:
        if not executable.is_file():
            parser.error(f"build release example first: {executable}")
    package = wheel_root()
    env = dict(os.environ, PYTHONPATH=str(package), PYTHONHASHSEED="0")
    evidence = provenance(package, [binary, baseline], include_baseline=True)
    cases = [("identity", 10000), ("record", 100), ("record", 1000),
             ("replay", 100), ("replay", 1000), ("hybrid",100), ("hybrid",1000), ("walk", 1000), ("walk", 10000)]
    if args.quick:
        cases = [("identity", 1000), ("record", 100), ("replay", 100), ("walk", 1000)]
    results = []
    rng = random.Random(160)
    for operation, size in cases:
        engines = [("python-1.6.0", [sys.executable, str(Path(__file__).with_name("benchmark_python.py"))]),
                   ("rust-0.1.0", [str(baseline)]), ("rust-updated", [str(binary)])]
        rng.shuffle(engines)
        records = {}
        for name, command in engines:
            print(f"{name}: {operation} {size}", flush=True)
            data = json.loads(output(command + [operation, str(size), str(args.samples)], env=env))
            assert data["operation"] == operation and data["size"] == size
            assert len(data["samples_seconds"]) == args.samples
            assert all(value > 0 for value in data["samples_seconds"])
            assert len(data["checksums"]) == args.samples and set(data["checksums"]) == {data["checksum"]}
            if name == "python-1.6.0":
                validate_python_origin(data, package)
            data["median_seconds"] = statistics.median(data["samples_seconds"])
            data["min_seconds"] = min(data["samples_seconds"])
            data["max_seconds"] = max(data["samples_seconds"])
            records[name] = data
        assert len({r["checksum"] for r in records.values()}) == 1, (operation, records)
        rust = records["rust-updated"]["median_seconds"]
        results.append({"operation": operation, "size": size, "engines": records,
                        "speedup_vs_python": records["python-1.6.0"]["median_seconds"] / rust,
                        "speedup_vs_rust_0_1_0": records["rust-0.1.0"]["median_seconds"] / rust})
        # Save incrementally so a long interrupted run retains evidence.
        document = {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "pypi_version": "1.6.0", "wheel_sha256": WHEEL_HASH,
                    **evidence,
                    "platform": platform.platform(), "processor": platform.processor(),
                    "logical_cpus": os.cpu_count(), "python": sys.version,
                    "method": {"build": "release", "warmups": 2, "samples": args.samples,
                               "seed": 160, "setup_and_startup_timed": False,
                               "offline": True, "checksum_equality_required": True},
                    "source_unchanged_during_run": source_fingerprints() == evidence["source_sha256"],
                    "results": results}
        assert document["source_unchanged_during_run"], "sources changed while timing"
        assert fingerprint([binary, baseline]) == evidence["binary_sha256"], "binaries changed while timing"
        args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    for case in results:
        print(f'{case["operation"]:10} {case["size"]:6} '
              f'Python/Rust {case["speedup_vs_python"]:.2f}x; '
              f'old/new Rust {case["speedup_vs_rust_0_1_0"]:.2f}x')


if __name__ == "__main__":
    main()
