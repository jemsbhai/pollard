"""Measure bounded offline Pollard per-step overhead for local stores and modes."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

import pollard
from pollard import MemoryStore, Run, Runtime, SQLiteStore, Store

DEFAULT_STEPS = 40
DEFAULT_SAMPLES = 15
DEFAULT_WARMUPS = 2
BACKENDS = ("memory", "sqlite")
MODES = ("record", "hybrid", "replay")
RUN_LABEL = "exp-007-overhead"
PAYLOAD = {
    "model": "deterministic-local",
    "messages": [{"role": "user", "content": "fixed offline input"}],
}
RESULT = {
    "text": "fixed offline result",
    "usage": {"input_tokens": 4, "output_tokens": 4},
}


class CountingModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return RESULT


class UnexpectedModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("a recorded hit executed the model callable")


def _digest_sources(sources: Iterable[tuple[str, bytes]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for relative, source in sources:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source)
        digest.update(b"\0")
        count += 1
    return digest.hexdigest(), count


def _workload_digest() -> str:
    text = json.dumps(
        {"payload": PAYLOAD, "result": RESULT},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _runner_source_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _package_source_digest() -> tuple[str, int]:
    package_file = getattr(pollard, "__file__", None)
    if not isinstance(package_file, str):
        raise RuntimeError("cannot locate the loaded Pollard package source")
    package_root = Path(package_file).resolve().parent
    paths = sorted(
        package_root.rglob("*.py"),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    return _digest_sources(
        (path.relative_to(package_root).as_posix(), path.read_bytes()) for path in paths
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_provenance(
    wheel: Path,
    loaded_source_sha256: str,
    loaded_source_file_count: int,
) -> dict[str, Any]:
    wheel = wheel.resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("--package-wheel must name an existing .whl file")

    with zipfile.ZipFile(wheel) as archive:
        source_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("pollard/") and name.endswith(".py")
        )
        wheel_source_sha256, wheel_source_file_count = _digest_sources(
            (name.removeprefix("pollard/"), archive.read(name)) for name in source_names
        )
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one dist-info/METADATA file")
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_names[0]))

    version = metadata["Version"]
    if version != pollard.__version__:
        raise ValueError(
            f"wheel version {version!r} does not match loaded Pollard {pollard.__version__!r}"
        )
    if (
        wheel_source_sha256 != loaded_source_sha256
        or wheel_source_file_count != loaded_source_file_count
    ):
        raise ValueError("loaded Pollard sources do not match --package-wheel")

    return {
        "filename": wheel.name,
        "sha256": _file_digest(wheel),
        "version": version,
        "package_source_sha256": wheel_source_sha256,
        "package_source_file_count": wheel_source_file_count,
        "matches_loaded_package_sources": True,
    }


def _loaded_distribution_provenance(package_wheel: Path | None) -> dict[str, Any]:
    try:
        installed = distribution("pollard")
    except PackageNotFoundError:
        return {
            "installer": None,
            "editable": None,
            "archive_sha256": None,
            "isolated_imports": bool(sys.flags.isolated),
            "loaded_from_distribution": False,
            "matches_package_wheel": False,
        }
    installer_text = installed.read_text("INSTALLER")
    direct_url_text = installed.read_text("direct_url.json")
    editable: bool | None = None
    archive_sha256: str | None = None
    if direct_url_text is not None:
        try:
            direct_url = json.loads(direct_url_text)
        except json.JSONDecodeError:
            direct_url = None
        if isinstance(direct_url, dict):
            directory = direct_url.get("dir_info")
            if isinstance(directory, dict):
                editable_value = directory.get("editable")
                if isinstance(editable_value, bool):
                    editable = editable_value
            archive = direct_url.get("archive_info")
            if isinstance(archive, dict):
                hashes = archive.get("hashes")
                if isinstance(hashes, dict) and isinstance(hashes.get("sha256"), str):
                    archive_sha256 = hashes["sha256"]
                legacy_hash = archive.get("hash")
                if archive_sha256 is None and isinstance(legacy_hash, str):
                    algorithm, separator, value = legacy_hash.partition("=")
                    if separator and algorithm.lower() == "sha256":
                        archive_sha256 = value
    expected_sha256 = (
        _file_digest(package_wheel.resolve()) if package_wheel is not None else None
    )
    package_file = getattr(pollard, "__file__", None)
    loaded_package_root = (
        Path(package_file).resolve().parent if isinstance(package_file, str) else None
    )
    distribution_package_root = Path(installed.locate_file("pollard")).resolve()
    loaded_from_distribution = loaded_package_root == distribution_package_root
    return {
        "installer": installer_text.strip() if installer_text is not None else None,
        "editable": editable,
        "archive_sha256": archive_sha256,
        "isolated_imports": bool(sys.flags.isolated),
        "loaded_from_distribution": loaded_from_distribution,
        "matches_package_wheel": (
            editable is not True
            and expected_sha256 is not None
            and archive_sha256 == expected_sha256
        ),
    }


def _repository_state(
    loaded_source_sha256: str,
    loaded_source_file_count: int,
) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[1]
    package_root = repository / "src" / "pollard"
    repository_source_sha256: str | None = None
    repository_source_file_count: int | None = None
    if package_root.is_dir():
        paths = sorted(
            package_root.rglob("*.py"),
            key=lambda path: path.relative_to(package_root).as_posix(),
        )
        repository_source_sha256, repository_source_file_count = _digest_sources(
            (path.relative_to(package_root).as_posix(), path.read_bytes()) for path in paths
        )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        top_level = git("rev-parse", "--show-toplevel")
        commit = git("rev-parse", "HEAD")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
    except OSError:
        root_matches_runner_repository: bool | None = None
        commit_value: str | None = None
        dirty: bool | None = None
    else:
        root_matches_runner_repository = (
            top_level.returncode == 0
            and Path(top_level.stdout.strip()).resolve() == repository.resolve()
        )
        if (
            not root_matches_runner_repository
            or commit.returncode != 0
            or status.returncode != 0
        ):
            commit_value = None
            dirty = None
        else:
            commit_value = commit.stdout.strip()
            dirty = bool(status.stdout.strip())
    return {
        "root_matches_runner_repository": root_matches_runner_repository,
        "commit": commit_value,
        "dirty": dirty,
        "package_source_sha256": repository_source_sha256,
        "package_source_file_count": repository_source_file_count,
        "matches_loaded_package_sources": (
            repository_source_sha256 == loaded_source_sha256
            and repository_source_file_count == loaded_source_file_count
        ),
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "minimum": round(min(values), 3),
        "p50": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "maximum": round(max(values), 3),
    }


def _measure_direct(steps: int) -> tuple[float, int]:
    model = CountingModel()
    last: dict[str, Any] | None = None
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        started = time.perf_counter_ns()
        for _ in range(steps):
            last = model(PAYLOAD)
        elapsed_ns = time.perf_counter_ns() - started
    finally:
        if gc_was_enabled:
            gc.enable()
    if last != RESULT or model.calls != steps:
        raise AssertionError("direct-call baseline did not complete the fixed workload")
    return elapsed_ns / steps / 1_000.0, model.calls


def _measure_pollard(run: Run, mode: str, steps: int) -> tuple[float, int]:
    model: CountingModel | UnexpectedModel
    model = CountingModel() if mode == "record" else UnexpectedModel()
    last_result: Any = None
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        started = time.perf_counter_ns()
        for _ in range(steps):
            last_result = run.model_call(PAYLOAD, fn=model).result
        elapsed_ns = time.perf_counter_ns() - started
    finally:
        if gc_was_enabled:
            gc.enable()
    if last_result != RESULT:
        raise AssertionError("Pollard did not return the fixed workload result")
    return elapsed_ns / steps / 1_000.0, model.calls


def _record_fixture(store: Store, steps: int) -> str:
    model = CountingModel()
    run = Runtime(store, mode="record").run(RUN_LABEL)
    for _ in range(steps):
        run.model_call(PAYLOAD, fn=model)
    if model.calls != steps:
        raise AssertionError("fixture recording did not execute every step")
    return run.root_id


def _prepared_run(
    backend: str,
    mode: str,
    steps: int,
    sqlite_path: Path,
) -> tuple[Store, Run]:
    if backend == "memory":
        store: Store = MemoryStore()
        if mode != "record":
            _record_fixture(store, steps)
    elif backend == "sqlite":
        if mode != "record":
            with SQLiteStore(sqlite_path) as fixture_store:
                _record_fixture(fixture_store, steps)
        store = SQLiteStore(sqlite_path, read_only=mode == "replay")
    else:
        raise ValueError(f"unsupported backend: {backend}")
    return store, Runtime(store, mode=mode).run(RUN_LABEL)


def _close_store(store: Store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _sample(
    backend: str,
    mode: str,
    steps: int,
    sample_index: int,
    directory: Path,
) -> dict[str, Any]:
    sqlite_path = directory / f"{backend}-{mode}-{sample_index}.db"
    store, run = _prepared_run(backend, mode, steps, sqlite_path)
    order = "baseline_then_pollard" if sample_index % 2 == 0 else "pollard_then_baseline"
    try:
        if order == "baseline_then_pollard":
            baseline_us, baseline_calls = _measure_direct(steps)
            pollard_us, pollard_calls = _measure_pollard(run, mode, steps)
        else:
            pollard_us, pollard_calls = _measure_pollard(run, mode, steps)
            baseline_us, baseline_calls = _measure_direct(steps)
        node_count = sum(1 for _node in store.walk(run.root_id))
    finally:
        _close_store(store)

    expected_pollard_calls = steps if mode == "record" else 0
    expected_node_count = steps + 1
    if baseline_calls != steps:
        raise AssertionError("baseline callable count changed")
    if pollard_calls != expected_pollard_calls:
        raise AssertionError(f"unexpected Pollard callable count for {backend}/{mode}")
    if node_count != expected_node_count:
        raise AssertionError(f"unexpected node count for {backend}/{mode}")

    return {
        "order": order,
        "baseline_calls": baseline_calls,
        "pollard_callable_calls": pollard_calls,
        "node_count": node_count,
        "baseline_mean_per_step_us": round(baseline_us, 3),
        "pollard_mean_per_step_us": round(pollard_us, 3),
        "incremental_over_direct_call_mean_per_step_us": round(
            pollard_us - baseline_us,
            3,
        ),
    }


def _run_case(
    backend: str,
    mode: str,
    steps: int,
    samples: int,
    warmups: int,
    directory: Path,
) -> dict[str, Any]:
    measured: list[dict[str, Any]] = []
    for sample_index in range(warmups + samples):
        row = _sample(backend, mode, steps, sample_index, directory)
        if sample_index >= warmups:
            measured.append({"sample": sample_index - warmups + 1, **row})

    metric_names = (
        "baseline_mean_per_step_us",
        "pollard_mean_per_step_us",
        "incremental_over_direct_call_mean_per_step_us",
    )
    return {
        "backend": backend,
        "mode": mode,
        "identity_condition": "fresh_chain" if mode == "record" else "recorded_chain_hit",
        "expected_pollard_callable_calls_per_sample": steps if mode == "record" else 0,
        "expected_node_count_per_sample": steps + 1,
        "invariants_passed": all(
            row["baseline_calls"] == steps
            and row["pollard_callable_calls"] == (steps if mode == "record" else 0)
            and row["node_count"] == steps + 1
            for row in measured
        ),
        "samples": measured,
        "summary_us": {
            name: _distribution([float(row[name]) for row in measured])
            for name in metric_names
        },
    }


def run_experiment(
    *,
    steps: int = DEFAULT_STEPS,
    samples: int = DEFAULT_SAMPLES,
    warmups: int = DEFAULT_WARMUPS,
    package_wheel: Path | None = None,
    require_publishable_provenance: bool = False,
) -> dict[str, Any]:
    if steps < 1 or samples < 2 or warmups < 0:
        raise ValueError("steps must be positive, samples at least two, and warmups nonnegative")

    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pollard-exp-007-") as temporary:
        directory = Path(temporary)
        for backend in BACKENDS:
            for mode in MODES:
                cases.append(_run_case(backend, mode, steps, samples, warmups, directory))

    clock = time.get_clock_info("perf_counter")
    passed = len(cases) == len(BACKENDS) * len(MODES) and all(
        case["invariants_passed"] for case in cases
    )
    package_source_sha256, package_source_file_count = _package_source_digest()
    wheel = (
        _wheel_provenance(
            package_wheel,
            package_source_sha256,
            package_source_file_count,
        )
        if package_wheel is not None
        else None
    )
    repository = _repository_state(
        package_source_sha256,
        package_source_file_count,
    )
    loaded_distribution = _loaded_distribution_provenance(package_wheel)
    commit = repository["commit"]
    publishable = (
        wheel is not None
        and isinstance(commit, str)
        and len(commit) == 40
        and repository["root_matches_runner_repository"] is True
        and repository["dirty"] is False
        and repository["matches_loaded_package_sources"] is True
        and loaded_distribution["matches_package_wheel"] is True
        and loaded_distribution["loaded_from_distribution"] is True
        and loaded_distribution["isolated_imports"] is True
    )
    if require_publishable_provenance and not publishable:
        raise ValueError(
            "publishable provenance requires an exact wheel, a known clean Git commit, "
            "an isolated interpreter loading that exact wheel installation, and matching "
            "checkout, wheel, and loaded-package sources"
        )
    return {
        "schema": "pollard/exp-007-overhead/v1",
        "id": "EXP-007",
        "status": "passed" if passed else "failed",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "question": (
            "What incremental per-step latency does Pollard add to a fixed local "
            "callable for MemoryStore and SQLite record, hybrid-hit, and replay-hit paths?"
        ),
        "protocol": {
            "backends": list(BACKENDS),
            "modes": list(MODES),
            "steps_per_sample": steps,
            "measured_samples": samples,
            "warmup_samples": warmups,
            "workload": {"payload": PAYLOAD, "result": RESULT},
            "workload_sha256": _workload_digest(),
            "baseline": "the same fixed Python callable invoked directly in a loop",
            "record_semantics": (
                "every record sample starts with an empty store and writes a fresh growing "
                "chain; hybrid and replay samples use a separately prepared matching chain"
            ),
            "timed_region": (
                "only the direct-call loop or Run.model_call loop; runtime construction, "
                "fixture recording, store opening, validation, and cleanup are excluded"
            ),
            "sample_statistic": "elapsed batch time divided by steps in that batch",
            "order": "direct and Pollard regions alternate first position by sample",
            "percentiles": "linear interpolation over measured batch-mean samples",
            "garbage_collection": "disabled only inside each timed region",
            "acceptance": (
                "schema and workload invariants only; timing values have no pass threshold"
            ),
        },
        "environment": {
            "python": sys.version.split()[0],
            "python_implementation": platform.python_implementation(),
            "python_compiler": platform.python_compiler(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "logical_cpu_count": os.cpu_count(),
            "pollard": pollard.__version__,
            "sqlite": sqlite3.sqlite_version,
            "clock": {
                "implementation": clock.implementation,
                "monotonic": clock.monotonic,
                "adjustable": clock.adjustable,
                "resolution_seconds": clock.resolution,
            },
        },
        "provenance": {
            "status": "release-bound" if publishable else "unbound",
            "publishable": publishable,
            "runner": "examples/exp_007_overhead.py",
            "runner_source_sha256": _runner_source_digest(),
            "runner_repository": repository,
            "package_source": "loaded Pollard package Python sources",
            "package_source_sha256": package_source_sha256,
            "package_source_file_count": package_source_file_count,
            "package_wheel": wheel,
            "loaded_distribution": loaded_distribution,
        },
        "network_used": False,
        "provider_spend_usd": 0,
        "cases": cases,
        "limitations": [
            "One local process and one local machine; no concurrency or remote store claim.",
            "The callable is deterministic and near-zero latency; no provider, adapter, "
            "network, tokenization, or model time is represented.",
            "Per-step values are batch means along a growing tree, not constant-depth "
            "independent-call latency measurements.",
            "Strict replay verifies recorded ancestry, so its cost depends on tree depth.",
            "SQLite results depend on filesystem, cache, antivirus, power, and scheduler state.",
            "This single run is descriptive evidence, not a latency guarantee or benchmark "
            "for another machine or workload.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument(
        "--package-wheel",
        type=Path,
        help="bind the result to this wheel and verify its sources are loaded",
    )
    parser.add_argument(
        "--require-publishable-provenance",
        action="store_true",
        help="fail unless wheel, loaded sources, and a clean Git checkout are bound",
    )
    parser.add_argument("--output", type=Path, help="write the JSON result to this path")
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    if args.samples < 2:
        parser.error("--samples must be at least two")
    if args.warmups < 0:
        parser.error("--warmups must be nonnegative")

    rendered = json.dumps(
        run_experiment(
            steps=args.steps,
            samples=args.samples,
            warmups=args.warmups,
            package_wheel=args.package_wheel,
            require_publishable_provenance=args.require_publishable_provenance,
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
