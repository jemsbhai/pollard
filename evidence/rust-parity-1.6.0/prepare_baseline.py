"""Restore the pinned Rust comparison source and copy the identical timing worker.

Writes only .benchmarks/rust-baseline, retaining cached target build outputs.
"""
import io
from pathlib import Path, PurePosixPath
import subprocess
import zipfile
from benchmark import ROOT, BASELINE_COMMIT

destination = ROOT / ".benchmarks/rust-baseline"
destination.mkdir(parents=True, exist_ok=True)
archive = subprocess.check_output(["git", "archive", "--format=zip", BASELINE_COMMIT,
                                   "crates/pollardai"], cwd=ROOT)
with zipfile.ZipFile(io.BytesIO(archive)) as files:
    for entry in files.infolist():
        name = PurePosixPath(entry.filename)
        assert not name.is_absolute() and ".." not in name.parts
        assert name.parts[:2] == ("crates", "pollardai") or entry.is_dir()
        target = destination.joinpath(*name.parts)
        assert target.resolve().is_relative_to(destination.resolve())
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files.read(entry))
worker = ROOT / "crates/pollardai/examples/performance.rs"
target = destination / "crates/pollardai/examples/performance.rs"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(worker.read_bytes())
print(f"Prepared Rust baseline {BASELINE_COMMIT}: {destination}")
