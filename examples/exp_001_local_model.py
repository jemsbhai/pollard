"""Run the formal EXP-001 local llama.cpp shared-prefix experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pollard
from pollard import Budget, MemoryStore, Runtime
from pollard.meters import StepMeter, TokenMeter, WallClockMeter
from pollard.meters.energy import EnergyMeter

SEEDS = tuple(range(5))
BRANCH_COUNTS = (2, 4, 8)
T_95_DF4 = 2.776
PREFIX_TOKENS = 24
SUFFIX_TOKENS = 16
IDLE_BASELINE_SECONDS = 5.0

QUESTIONS = (
    "Name the budget invariant in six words.",
    "Name the replay invariant in six words.",
    "Name the registry invariant in six words.",
    "Name the identity invariant in six words.",
    "Name the storage invariant in six words.",
    "Name the merge invariant in six words.",
    "Name the seal invariant in six words.",
    "Name the window invariant in six words.",
)


def offline_dossier() -> str:
    facts = (
        "A governed run records semantic steps beneath a content-addressed root. "
        "A budget precheck may refuse a known charge before a function executes. "
        "Actual provider usage is settled after a successful call. "
        "A branch keeps its shared ancestry while allowing a different continuation. "
        "Replay serves a stored result only after verifying the stored node. "
        "A registered action resolves by name and version before its handler runs. "
        "Canonical JSON and parent identity determine stable node identifiers. "
        "Payload interning changes storage encoding without changing identity. "
        "Store merge retains conflicts instead of silently choosing a winner. "
        "A seal commits to node identifiers and result digests in traversal order. "
        "Sliding windows count settled events and active reservations. "
        "An expired lease returns capacity, but a late call may still have spent money."
    )
    return "\n".join(f"Section {index}: {facts}" for index in range(1, 7))


def prefix_prompt() -> str:
    return (
        "Read the fixed dossier. Return one compact capsule of at most 24 words that "
        "preserves its operational rules.\n\n" + offline_dossier() + "\n\nCapsule:"
    )


def suffix_prompt(capsule: str, branch: int) -> str:
    return (
        f"Context capsule: {capsule.strip()}\n"
        f"Question: {QUESTIONS[branch]}\n"
        "Answer with only the requested six words:"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LlamaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def completion(
        self,
        prompt: str,
        *,
        seed: int,
        n_predict: int,
    ) -> dict[str, Any]:
        response = self._request(
            "/completion",
            {
                "prompt": prompt,
                "seed": seed,
                "n_predict": n_predict,
                "temperature": 0.2,
                "top_k": 20,
                "top_p": 0.9,
                "cache_prompt": False,
                "stream": False,
            },
        )
        timings = response.get("timings")
        if not isinstance(timings, dict):
            raise RuntimeError("llama.cpp response omitted timings")
        cache_n = _required_int(timings, "cache_n")
        prompt_n = _required_int(timings, "prompt_n")
        predicted_n = _required_int(timings, "predicted_n")
        content = response.get("content")
        if not isinstance(content, str):
            raise RuntimeError("llama.cpp response omitted string content")
        return {
            "text": content,
            "usage": {"input_tokens": prompt_n + cache_n, "output_tokens": predicted_n},
            "llama_timings": {
                "cache_n": cache_n,
                "prompt_n": prompt_n,
                "predicted_n": predicted_n,
                "prompt_ms": _required_number(timings, "prompt_ms"),
                "predicted_ms": _required_number(timings, "predicted_ms"),
            },
        }

    def get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"llama.cpp {path} returned a non-object")
        return value

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("llama.cpp completion returned a non-object")
        return value


class NvmlCounter:
    def __init__(self, index: int = 0) -> None:
        import pynvml

        pynvml.nvmlInit()
        self.nvml = pynvml
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(index)

    def energy_mj(self) -> int:
        value = self.nvml.nvmlDeviceGetTotalEnergyConsumption(self.handle)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("NVML cumulative energy counter returned a non-integer")
        return value

    def environment(self) -> dict[str, Any]:
        memory = self.nvml.nvmlDeviceGetMemoryInfo(self.handle)
        return {
            "name": _decode(self.nvml.nvmlDeviceGetName(self.handle)),
            "driver": _decode(self.nvml.nvmlSystemGetDriverVersion()),
            "memory_bytes": int(memory.total),
            "energy_source": "nvmlDeviceGetTotalEnergyConsumption",
            "energy_counter_unit": "millijoules",
            "scope": "whole GPU, including other processes",
        }


@contextmanager
def measured_condition(counter: NvmlCounter) -> Iterator[dict[str, float]]:
    readings: dict[str, float] = {}
    start_energy = counter.energy_mj()
    started = time.perf_counter()
    yield readings
    readings["wallclock_seconds"] = time.perf_counter() - started
    end_energy = counter.energy_mj()
    if end_energy <= start_energy:
        raise RuntimeError("NVML cumulative energy counter did not advance")
    readings["nvml_joules"] = (end_energy - start_energy) / 1000.0


def measure_idle_watts(counter: NvmlCounter, seconds: float) -> float:
    start = counter.energy_mj()
    started = time.perf_counter()
    time.sleep(seconds)
    duration = time.perf_counter() - started
    end = counter.energy_mj()
    return (end - start) / 1000.0 / duration


def _model_fn(
    client: LlamaClient,
    *,
    seed: int,
    n_predict: int,
) -> Any:
    def call(payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("EXP-001 model payload omitted prompt")
        return client.completion(prompt, seed=seed, n_predict=n_predict)

    return call


def run_condition(
    client: LlamaClient,
    counter: NvmlCounter,
    *,
    condition: str,
    branches: int,
    seed: int,
    model_id: str,
    usd_per_kwh: Decimal,
    idle_watts: float,
) -> dict[str, Any]:
    energy_meter = EnergyMeter(index=0, interval_s=0.05)
    runtime = Runtime(
        MemoryStore(),
        meters=[StepMeter(), TokenMeter(), WallClockMeter(), energy_meter],
    )
    run = runtime.run(
        f"exp-001-{condition}-n{branches}-seed{seed}",
        budget=Budget(steps=branches * 2 + 2),
    )
    outputs: list[str] = []
    prefix_outputs: list[str] = []
    cache_counts: list[int] = []

    def record(node: Any, *, prefix: bool) -> str:
        result = node.result
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("EXP-001 stored result has an invalid shape")
        text = str(result["text"])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if prefix:
            prefix_outputs.append(digest)
        else:
            outputs.append(digest)
        timings = result.get("llama_timings")
        if not isinstance(timings, dict):
            raise RuntimeError("EXP-001 stored result omitted llama timings")
        cache_counts.append(_required_int(timings, "cache_n"))
        return text

    with measured_condition(counter) as measured:
        if condition == "shared":
            prefix_node = run.model_call(
                {
                    "model": model_id,
                    "stage": "prefix",
                    "prompt": prefix_prompt(),
                    "seed": seed,
                },
                fn=_model_fn(client, seed=seed, n_predict=PREFIX_TOKENS),
            )
            capsule = record(prefix_node, prefix=True)
            for branch in range(branches):
                with run.branch(attempt=branch) as child:
                    node = child.model_call(
                        {
                            "model": model_id,
                            "stage": "suffix",
                            "branch": branch,
                            "prompt": suffix_prompt(capsule, branch),
                            "seed": seed * 100 + branch + 1,
                        },
                        fn=_model_fn(
                            client,
                            seed=seed * 100 + branch + 1,
                            n_predict=SUFFIX_TOKENS,
                        ),
                    )
                    record(node, prefix=False)
        elif condition == "naive":
            for branch in range(branches):
                with run.branch(attempt=branch) as child:
                    prefix_node = child.model_call(
                        {
                            "model": model_id,
                            "stage": "prefix",
                            "branch": branch,
                            "prompt": prefix_prompt(),
                            "seed": seed,
                        },
                        fn=_model_fn(client, seed=seed, n_predict=PREFIX_TOKENS),
                    )
                    capsule = record(prefix_node, prefix=True)
                    node = child.model_call(
                        {
                            "model": model_id,
                            "stage": "suffix",
                            "branch": branch,
                            "prompt": suffix_prompt(capsule, branch),
                            "seed": seed * 100 + branch + 1,
                        },
                        fn=_model_fn(
                            client,
                            seed=seed * 100 + branch + 1,
                            n_predict=SUFFIX_TOKENS,
                        ),
                    )
                    record(node, prefix=False)
        else:
            raise ValueError(f"unknown EXP-001 condition: {condition}")

    report = run.report()
    raw_joules = measured["nvml_joules"]
    wallclock = measured["wallclock_seconds"]
    adjusted_joules = max(0.0, raw_joules - idle_watts * wallclock)
    return {
        "condition": condition,
        "branches": branches,
        "seed": seed,
        "calls": len(cache_counts),
        "wallclock_seconds": round(wallclock, 6),
        "nvml_joules": round(raw_joules, 6),
        "idle_adjusted_joules": round(adjusted_joules, 6),
        "usd_at_declared_energy_rate": _energy_usd(raw_joules, usd_per_kwh),
        "idle_adjusted_usd_at_declared_energy_rate": _energy_usd(adjusted_joules, usd_per_kwh),
        "pollard_meter_seconds": round(float(report["spent"]["seconds"]), 6),
        "pollard_meter_joules": round(float(report["spent"]["joules"]), 6),
        "tokens": int(report["spent"]["tokens"]),
        "max_llama_cache_n": max(cache_counts, default=0),
        "prefix_output_digests": prefix_outputs,
        "suffix_output_digests": outputs,
    }


def _energy_usd(joules: float, usd_per_kwh: Decimal) -> str:
    value = Decimal(str(joules)) / Decimal(3_600_000) * usd_per_kwh
    return format(value.quantize(Decimal("0.000000000001")), "f")


def mean_ci95(values: list[float]) -> dict[str, float]:
    mean = statistics.mean(values)
    half_width = 0.0
    if len(values) > 1:
        half_width = T_95_DF4 * statistics.stdev(values) / math.sqrt(len(values))
    return {"mean": round(mean, 6), "ci95_half_width": round(half_width, 6)}


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    metrics = (
        "wallclock_seconds",
        "nvml_joules",
        "idle_adjusted_joules",
        "usd_at_declared_energy_rate",
        "idle_adjusted_usd_at_declared_energy_rate",
        "tokens",
    )
    for branches in BRANCH_COUNTS:
        paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for seed in SEEDS:
            shared = next(
                row
                for row in rows
                if row["branches"] == branches
                and row["seed"] == seed
                and row["condition"] == "shared"
            )
            naive = next(
                row
                for row in rows
                if row["branches"] == branches
                and row["seed"] == seed
                and row["condition"] == "naive"
            )
            paired.append((shared, naive))
        condition_summary = {
            condition: {
                metric: mean_ci95(
                    [
                        float(shared[metric] if condition == "shared" else naive[metric])
                        for shared, naive in paired
                    ]
                )
                for metric in metrics
            }
            for condition in ("shared", "naive")
        }
        savings = {
            metric: mean_ci95(
                [
                    (1.0 - float(shared[metric]) / float(naive[metric])) * 100.0
                    for shared, naive in paired
                ]
            )
            for metric in metrics
        }
        output_parity = all(
            len(set(naive["prefix_output_digests"])) == 1
            and shared["prefix_output_digests"][0] == naive["prefix_output_digests"][0]
            and shared["suffix_output_digests"] == naive["suffix_output_digests"]
            for shared, naive in paired
        )
        summaries.append(
            {
                "branches": branches,
                "seeds": len(paired),
                "conditions": condition_summary,
                "shared_savings_pct": savings,
                "output_digest_parity": output_parity,
            }
        )
    return summaries


def load_price_table(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        try:
            import tomli as tomllib
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency message
            raise RuntimeError("Python 3.10 requires tomli to read the price table") from exc
    with path.open("rb") as stream:
        return tomllib.load(stream)


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    runner_started = time.perf_counter()
    runtime_sha256 = sha256_file(args.runtime_archive)
    model_sha256 = sha256_file(args.model)
    price_sha256 = sha256_file(args.price_table)
    _verify_hash("runtime archive", runtime_sha256, args.expected_runtime_sha256)
    _verify_hash("model", model_sha256, args.expected_model_sha256)
    prices = load_price_table(args.price_table)
    energy_price = prices.get("local_energy")
    if not isinstance(energy_price, dict):
        raise ValueError("price table omitted [local_energy]")
    usd_per_kwh = Decimal(str(energy_price["usd_per_kwh"]))

    counter = NvmlCounter()
    server_version = subprocess.run(
        [str(args.server_binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    with llama_server(args) as client:
        warmup: list[dict[str, Any]] = []
        for seed in range(3):
            result = client.completion(
                "Return exactly two words: warm cache",
                seed=seed,
                n_predict=4,
            )
            warmup.append(result["llama_timings"])
        idle_watts = measure_idle_watts(counter, IDLE_BASELINE_SECONDS)
        props = client.get("/props")
        schedule = [(branches, seed) for branches in BRANCH_COUNTS for seed in SEEDS]
        random.Random(9001).shuffle(schedule)
        rows: list[dict[str, Any]] = []
        rendered_schedule: list[dict[str, Any]] = []
        for branches, seed in schedule:
            order = ("shared", "naive") if (branches + seed) % 2 == 0 else ("naive", "shared")
            rendered_schedule.append(
                {"branches": branches, "seed": seed, "condition_order": list(order)}
            )
            for condition in order:
                rows.append(
                    run_condition(
                        client,
                        counter,
                        condition=condition,
                        branches=branches,
                        seed=seed,
                        model_id=args.model_id,
                        usd_per_kwh=usd_per_kwh,
                        idle_watts=idle_watts,
                    )
                )

    summary = summarize(rows)
    passed = (
        all(row["max_llama_cache_n"] == 0 for row in rows)
        and all(item["output_digest_parity"] for item in summary)
        and all(
            item["shared_savings_pct"]["wallclock_seconds"]["mean"] > 0
            and item["shared_savings_pct"]["nvml_joules"]["mean"] > 0
            for item in summary
        )
    )
    return {
        "id": "EXP-001",
        "leg": "local_model",
        "status": "passed" if passed else "failed",
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "runner_duration_seconds": round(time.perf_counter() - runner_started, 6),
        "question": (
            "What wall-clock, whole-GPU energy, and declared-rate electricity cost "
            "change follows from executing one shared prefix instead of repeating it?"
        ),
        "protocol": {
            "branch_counts": list(BRANCH_COUNTS),
            "seeds": list(SEEDS),
            "prefix_max_output_tokens": PREFIX_TOKENS,
            "suffix_max_output_tokens": SUFFIX_TOKENS,
            "prompt_cache": False,
            "server_parallel_slots": 1,
            "schedule": rendered_schedule,
            "confidence_interval": "two-sided 95% Student t interval, df=4",
            "wallclock_scope": (
                "condition calls only; model load, warmup, and idle baseline excluded"
            ),
            "energy_scope": "raw NVML whole-GPU cumulative counter over condition wallclock",
            "cost_formula": "nvml_joules / 3,600,000 * declared usd_per_kwh",
            "comparison_limit": (
                "electricity-only scenario; excludes host, capital, cooling, and labor cost"
            ),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pollard": pollard.__version__,
            "gpu": counter.environment(),
            "idle_baseline_seconds": IDLE_BASELINE_SECONDS,
            "idle_baseline_watts": round(idle_watts, 6),
            "llama_cpp_release": args.llama_release,
            "llama_cpp_version_output": (server_version.stdout + server_version.stderr).strip(),
            "llama_cpp_runtime_archive_sha256": runtime_sha256,
            "model_id": args.model_id,
            "model_sha256": model_sha256,
            "model_bytes": args.model.stat().st_size,
            "model_properties": _selected_props(props),
            "price_table_sha256": price_sha256,
            "price_table": energy_price,
            "warmup_timings": warmup,
        },
        "rows": rows,
        "summary": summary,
    }


@contextmanager
def llama_server(args: argparse.Namespace) -> Iterator[LlamaClient]:
    base_url = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="pollard-exp-001-") as temporary:
        log_path = Path(temporary) / "llama-server.log"
        with log_path.open("w", encoding="utf-8") as log:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [
                    str(args.server_binary),
                    "-m",
                    str(args.model),
                    "-ngl",
                    "99",
                    "-c",
                    "4096",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.port),
                    "--parallel",
                    "1",
                    "--no-cache-prompt",
                    "--cache-ram",
                    "0",
                    "--no-cache-idle-slots",
                    "--no-webui",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=os.environ.copy(),
            )
            try:
                client = LlamaClient(base_url)
                _wait_for_server(client, process, log_path)
                yield client
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


def _wait_for_server(client: LlamaClient, process: subprocess.Popen[Any], log: Path) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited early:\n{log.read_text(encoding='utf-8')}")
        try:
            if client.get("/health").get("status") == "ok":
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"llama-server readiness timed out:\n{log.read_text(encoding='utf-8')}")


def _selected_props(props: dict[str, Any]) -> dict[str, Any]:
    selected = {}
    for key in ("model_alias", "n_ctx", "n_batch", "n_ubatch"):
        if key in props and isinstance(props[key], str | int | float | bool | None):
            selected[key] = props[key]
    return selected


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise RuntimeError(f"llama.cpp timings omitted integer {key}")
    return item


def _required_number(value: dict[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise RuntimeError(f"llama.cpp timings omitted number {key}")
    return float(item)


def _decode(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _verify_hash(label: str, actual: str, expected: str) -> None:
    if actual.lower() != expected.lower():
        raise RuntimeError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--llama-release", required=True)
    parser.add_argument("--expected-runtime-sha256", required=True)
    parser.add_argument("--expected-model-sha256", required=True)
    parser.add_argument("--price-table", type=Path, default=Path("evidence/prices.toml"))
    parser.add_argument("--port", type=int, default=8124)
    parser.add_argument("--output", type=Path, help="write the JSON result to this path")
    args = parser.parse_args()
    rendered = json.dumps(run_experiment(args), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
