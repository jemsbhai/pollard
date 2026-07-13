"""Run formal EXP-005 PostgreSQL contention and estimator-bound evidence."""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import platform
import queue
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pollard
from pollard import Budget, BudgetExceeded, PostgresStore, Runtime, WindowMeter
from pollard.meters import StepMeter, TokenMeter

WORKER_COUNTS = (2, 4, 8)
CALL_DURATIONS_MS = (0, 10, 50)
ESTIMATOR_PROFILES = {"below": (4, 2), "equal": (4, 4), "above": (4, 7)}
LEASE_SECONDS = (1.0, 2.0, 4.0)
ROUNDS = 30
FAILURE_SEEDS = 5


class PayloadEstimator:
    def estimate_input_tokens(self, payload: dict[str, object]) -> int:
        value = payload.get("estimate_tokens")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("EXP-005 payload is missing estimate_tokens")
        return value


def _call_delay(duration_ms: int, seed: int, worker: int, attempt: int) -> None:
    if duration_ms == 0:
        return
    variation = ((seed * 31 + worker * 17 + attempt * 13) % 51) / 100.0
    time.sleep(duration_ms / 1000.0 * (0.75 + variation))


def _exact_worker(
    dsn: str,
    store_id: str,
    meter_name: str,
    workers: int,
    rounds: int,
    duration_ms: int,
    barrier: Any,
    output: Any,
    worker: int,
) -> None:
    rows: list[dict[str, Any]] = []
    try:
        limit = workers * 2
        with PostgresStore(dsn, store_id=store_id) as store:
            for seed in range(rounds):
                if meter_name == "steps":
                    runtime = Runtime(store)
                    run = runtime.run(
                        f"exact-{meter_name}-{workers}-{duration_ms}-{seed}",
                        budget=Budget(steps=limit),
                    )
                else:
                    runtime = Runtime(
                        store,
                        meters=[StepMeter(), WindowMeter("requests", limit, 3600)],
                    )
                    run = runtime.run(
                        f"exact-{meter_name}-{workers}-{duration_ms}-{seed}"
                    )
                executed = 0
                refused = 0
                barrier.wait(timeout=60)
                for attempt in range(4):
                    try:
                        run.model_call(
                            {
                                "model": "synthetic",
                                "seed": seed,
                                "worker": worker,
                                "attempt": attempt,
                            },
                            fn=lambda _payload, attempt=attempt, seed=seed: _exact_call(
                                duration_ms, seed, worker, attempt
                            ),
                        )
                        executed += 1
                    except BudgetExceeded:
                        refused += 1
                        break
                barrier.wait(timeout=60)
                rows.append(
                    {"seed": seed, "executed": executed, "refused": refused}
                )
        output.put({"worker": worker, "rows": rows, "error": None})
    except Exception as exc:
        output.put(
            {
                "worker": worker,
                "rows": rows,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _exact_call(duration_ms: int, seed: int, worker: int, attempt: int) -> dict[str, Any]:
    _call_delay(duration_ms, seed, worker, attempt)
    return {"text": "ok", "usage": {"input_tokens": 0, "output_tokens": 0}}


def _estimated_worker(
    dsn: str,
    store_id: str,
    profile: str,
    workers: int,
    rounds: int,
    barrier: Any,
    output: Any,
    worker: int,
) -> None:
    rows: list[dict[str, Any]] = []
    estimate, actual = ESTIMATOR_PROFILES[profile]
    limit = estimate * workers * 2
    try:
        with PostgresStore(dsn, store_id=store_id) as store:
            for seed in range(rounds):
                runtime = Runtime(store, meters=[TokenMeter(PayloadEstimator())])
                run = runtime.run(
                    f"estimated-{profile}-{workers}-{seed}",
                    budget=Budget(tokens=limit),
                )
                executed = 0
                refused = 0
                barrier.wait(timeout=60)
                for attempt in range(4):
                    try:
                        run.model_call(
                            {
                                "model": "synthetic",
                                "estimate_tokens": estimate,
                                "seed": seed,
                                "worker": worker,
                                "attempt": attempt,
                            },
                            fn=lambda _payload, attempt=attempt, seed=seed: _token_call(
                                actual, seed, worker, attempt
                            ),
                        )
                        executed += 1
                    except BudgetExceeded:
                        refused += 1
                        break
                barrier.wait(timeout=60)
                rows.append(
                    {
                        "seed": seed,
                        "executed": executed,
                        "refused": refused,
                        "estimate_sum": executed * estimate,
                        "actual_sum": executed * actual,
                        "positive_error_sum": executed * max(0, actual - estimate),
                    }
                )
        output.put({"worker": worker, "rows": rows, "error": None})
    except Exception as exc:
        output.put(
            {
                "worker": worker,
                "rows": rows,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _token_call(actual: int, seed: int, worker: int, attempt: int) -> dict[str, Any]:
    _call_delay(10, seed, worker, attempt)
    return {
        "text": "ok",
        "usage": {"input_tokens": actual, "output_tokens": 0},
    }


def _abandoned_worker(
    dsn: str,
    store_id: str,
    label: str,
    lease_seconds: float,
    ready: Any,
) -> None:
    with PostgresStore(dsn, store_id=store_id) as store:
        runtime = Runtime(
            store,
            meters=[TokenMeter(PayloadEstimator())],
            reservation_lease_seconds=lease_seconds,
        )
        run = runtime.run(label, budget=Budget(tokens=4))

        def block(_payload: dict[str, Any]) -> dict[str, Any]:
            ready.set()
            time.sleep(30)
            return {"usage": {"input_tokens": 4, "output_tokens": 0}}

        run.model_call(
            {"model": "synthetic", "estimate_tokens": 4, "phase": "abandoned"},
            fn=block,
        )


def _process_group(
    target: Any,
    args: Sequence[Any],
    workers: int,
    *,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    context = mp.get_context("spawn")
    barrier = context.Barrier(workers)
    output = context.Queue()
    processes = [
        context.Process(target=target, args=(*args, barrier, output, worker))
        for worker in range(workers)
    ]
    for process in processes:
        process.start()
    received: list[dict[str, Any]] = []
    errors: list[str] = []
    deadline = time.monotonic() + timeout_s
    for _process in processes:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            received.append(output.get(timeout=remaining))
        except queue.Empty:
            errors.append("worker result timed out")
            break
    for process in processes:
        remaining = max(0.1, deadline - time.monotonic())
        process.join(timeout=remaining)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            errors.append(f"worker pid {process.pid} timed out")
        elif process.exitcode != 0:
            errors.append(f"worker pid {process.pid} exited {process.exitcode}")
    for result in received:
        if result.get("error"):
            errors.append(f"worker {result['worker']}: {result['error']}")
    output.close()
    return received, errors


def _reservation_counts(dsn: str, store_id: str) -> dict[str, int]:
    import psycopg

    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT
              count(*) FILTER (
                WHERE expires_at > EXTRACT(EPOCH FROM clock_timestamp())
              ),
              count(*) FILTER (
                WHERE expires_at <= EXTRACT(EPOCH FROM clock_timestamp())
              )
            FROM pollard_reservations WHERE store_id = %s
            """,
            (store_id,),
        ).fetchone()
    return {"active": int(row[0]), "expired": int(row[1])}


def _server_environment(dsn: str) -> dict[str, str]:
    import psycopg

    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT current_setting('server_version'), version()"
        ).fetchone()
    return {"server_version": str(row[0]), "server_build": str(row[1])}


def _combine_exact(
    received: list[dict[str, Any]],
    errors: list[str],
    rounds: int,
    limit: int,
    active_reservations: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    for seed in range(rounds):
        selected = [
            row
            for result in received
            for row in result["rows"]
            if row["seed"] == seed
        ]
        admitted = sum(int(row["executed"]) for row in selected)
        refusals = sum(int(row["refused"]) for row in selected)
        rows.append(
            {
                "seed": seed,
                "admitted": admitted,
                "settled": admitted,
                "refusals": refusals,
                "limit": limit,
                "bound_slack": limit - admitted,
                "saturated": admitted == limit,
                "within_bound": admitted <= limit,
            }
        )
    passed = (
        not errors
        and len(received) > 0
        and len(rows) == rounds
        and active_reservations == 0
        and all(row["within_bound"] and row["saturated"] for row in rows)
    )
    return rows, passed


def _combine_estimated(
    received: list[dict[str, Any]],
    errors: list[str],
    rounds: int,
    limit: int,
    active_reservations: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    for seed in range(rounds):
        selected = [
            row
            for result in received
            for row in result["rows"]
            if row["seed"] == seed
        ]
        admitted = sum(int(row["executed"]) for row in selected)
        refusals = sum(int(row["refused"]) for row in selected)
        estimated = sum(int(row["estimate_sum"]) for row in selected)
        actual = sum(int(row["actual_sum"]) for row in selected)
        positive_error = sum(int(row["positive_error_sum"]) for row in selected)
        upper_bound = limit + positive_error
        rows.append(
            {
                "seed": seed,
                "admitted": admitted,
                "refusals": refusals,
                "estimated_tokens": estimated,
                "settled_tokens": actual,
                "limit": limit,
                "positive_actual_minus_estimate": positive_error,
                "overshoot": max(0, actual - limit),
                "upper_bound": upper_bound,
                "bound_slack": upper_bound - actual,
                "within_bound": actual <= upper_bound,
            }
        )
    passed = (
        not errors
        and len(received) > 0
        and len(rows) == rounds
        and active_reservations == 0
        and all(row["within_bound"] for row in rows)
    )
    return rows, passed


def _distribution(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
        "mean": round(statistics.mean(ordered), 6),
    }


def _run_exact_conditions(
    label: str,
    dsn: str,
    session_id: str,
    rounds: int,
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for workers in WORKER_COUNTS:
        for duration_ms in CALL_DURATIONS_MS:
            for meter_name in ("steps", "requests"):
                store_id = (
                    f"exp005-{session_id}-{label}-exact-{meter_name}-{workers}-{duration_ms}"
                )
                received, errors = _process_group(
                    _exact_worker,
                    (dsn, store_id, meter_name, workers, rounds, duration_ms),
                    workers,
                    timeout_s=max(180, rounds * (duration_ms / 1000 * 8 + 2)),
                )
                reservations = _reservation_counts(dsn, store_id)
                rows, passed = _combine_exact(
                    received, errors, rounds, workers * 2, reservations["active"]
                )
                conditions.append(
                    {
                        "kind": "exact",
                        "meter": meter_name,
                        "workers": workers,
                        "call_duration_ms": duration_ms,
                        "rounds": rounds,
                        "reservations_after": reservations,
                        "database_errors": errors,
                        "rows": rows,
                        "summary": {
                            "admitted": _distribution(
                                [int(row["admitted"]) for row in rows]
                            ),
                            "refusals": _distribution(
                                [int(row["refusals"]) for row in rows]
                            ),
                        },
                        "passed": passed,
                    }
                )
    return conditions


def _run_estimated_conditions(
    label: str,
    dsn: str,
    session_id: str,
    rounds: int,
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for workers in WORKER_COUNTS:
        for profile, (estimate, _actual) in ESTIMATOR_PROFILES.items():
            store_id = f"exp005-{session_id}-{label}-estimated-{profile}-{workers}"
            received, errors = _process_group(
                _estimated_worker,
                (dsn, store_id, profile, workers, rounds),
                workers,
                timeout_s=max(180, rounds * 4),
            )
            reservations = _reservation_counts(dsn, store_id)
            limit = estimate * workers * 2
            rows, passed = _combine_estimated(
                received, errors, rounds, limit, reservations["active"]
            )
            conditions.append(
                {
                    "kind": "estimated",
                    "profile": profile,
                    "estimate_per_call": estimate,
                    "actual_per_call": ESTIMATOR_PROFILES[profile][1],
                    "workers": workers,
                    "call_duration_ms": 10,
                    "rounds": rounds,
                    "reservations_after": reservations,
                    "database_errors": errors,
                    "rows": rows,
                    "summary": {
                        "overshoot": _distribution(
                            [int(row["overshoot"]) for row in rows]
                        ),
                        "bound_slack": _distribution(
                            [int(row["bound_slack"]) for row in rows]
                        ),
                        "refusals": _distribution(
                            [int(row["refusals"]) for row in rows]
                        ),
                    },
                    "passed": passed,
                }
            )
    return conditions


def _run_failure_conditions(
    label: str,
    dsn: str,
    session_id: str,
    failure_seeds: int,
) -> list[dict[str, Any]]:
    context = mp.get_context("spawn")
    conditions: list[dict[str, Any]] = []
    for lease_seconds in LEASE_SECONDS:
        rows: list[dict[str, Any]] = []
        for seed in range(failure_seeds):
            store_id = f"exp005-{session_id}-{label}-failure-{lease_seconds}-{seed}"
            run_label = f"failure-{lease_seconds}-{seed}"
            ready = context.Event()
            process = context.Process(
                target=_abandoned_worker,
                args=(dsn, store_id, run_label, lease_seconds, ready),
            )
            process.start()
            signaled = ready.wait(timeout=30)
            process.terminate()
            process.join(timeout=10)
            before = _reservation_counts(dsn, store_id)
            pre_expiry_refused = False
            recovered = False
            error: str | None = None
            started = time.monotonic()
            try:
                with PostgresStore(dsn, store_id=store_id) as store:
                    runtime = Runtime(
                        store,
                        meters=[TokenMeter(PayloadEstimator())],
                        reservation_lease_seconds=lease_seconds,
                    )
                    run = runtime.run(run_label, budget=Budget(tokens=4))
                    try:
                        run.model_call(
                            {
                                "model": "synthetic",
                                "estimate_tokens": 4,
                                "phase": "before-expiry",
                            },
                            fn=lambda _payload: {
                                "usage": {"input_tokens": 4, "output_tokens": 0}
                            },
                        )
                    except BudgetExceeded:
                        pre_expiry_refused = True
                    time.sleep(lease_seconds + 0.2)
                    run.model_call(
                        {
                            "model": "synthetic",
                            "estimate_tokens": 4,
                            "phase": "after-expiry",
                        },
                        fn=lambda _payload: {
                            "usage": {"input_tokens": 4, "output_tokens": 0}
                        },
                    )
                    recovered = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            after = _reservation_counts(dsn, store_id)
            rows.append(
                {
                    "seed": seed,
                    "worker_entered_function": signaled,
                    "terminated_exitcode": process.exitcode,
                    "reservations_after_termination": before,
                    "pre_expiry_refused": pre_expiry_refused,
                    "recovered_on_first_post_expiry_precheck": recovered,
                    "reservations_after_recovery": after,
                    "expired_reservations_released": max(
                        0, before["active"] - after["active"]
                    ),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "error": error,
                }
            )
        passed = all(
            row["worker_entered_function"]
            and row["reservations_after_termination"]["active"] == 1
            and row["reservations_after_termination"]["expired"] == 0
            and row["pre_expiry_refused"]
            and row["recovered_on_first_post_expiry_precheck"]
            and row["reservations_after_recovery"]["active"] == 0
            and row["reservations_after_recovery"]["expired"] == 1
            and row["error"] is None
            for row in rows
        )
        conditions.append(
            {
                "kind": "abandoned_reservation",
                "lease_seconds": lease_seconds,
                "rounds": failure_seeds,
                "rows": rows,
                "passed": passed,
            }
        )
    return conditions


def run_experiment(
    targets: list[tuple[str, str]],
    *,
    rounds: int = ROUNDS,
    failure_seeds: int = FAILURE_SEEDS,
    target_images: dict[str, str] | None = None,
) -> dict[str, Any]:
    session_id = uuid4().hex[:12]
    target_results: list[dict[str, Any]] = []
    for label, dsn in targets:
        exact = _run_exact_conditions(label, dsn, session_id, rounds)
        estimated = _run_estimated_conditions(label, dsn, session_id, rounds)
        failures = _run_failure_conditions(label, dsn, session_id, failure_seeds)
        all_conditions = exact + estimated + failures
        target_results.append(
            {
                "label": label,
                "environment": {
                    **_server_environment(dsn),
                    "container_image": (target_images or {}).get(label),
                },
                "conditions": all_conditions,
                "passed": all(condition["passed"] for condition in all_conditions),
            }
        )
    passed = len(target_results) >= 2 and all(result["passed"] for result in target_results)
    return {
        "id": "EXP-005",
        "status": "passed" if passed else "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "question": (
            "Do exact and estimated shared limits obey their documented concurrency bounds?"
        ),
        "protocol": {
            "worker_processes": list(WORKER_COUNTS),
            "call_durations_ms": list(CALL_DURATIONS_MS),
            "rounds_per_contention_condition": rounds,
            "estimator_profiles": {
                name: {"estimate": values[0], "actual": values[1]}
                for name, values in ESTIMATOR_PROFILES.items()
            },
            "failure_lease_seconds": list(LEASE_SECONDS),
            "failure_rounds_per_lease": failure_seeds,
            "exact_bound": "settled <= configured limit",
            "estimated_bound": (
                "settled <= limit + sum(max(actual - estimate, 0)) over admitted calls"
            ),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pollard": pollard.__version__,
            "psycopg": _package_version("psycopg"),
        },
        "targets": target_results,
    }


def _package_version(name: str) -> str:
    from importlib.metadata import version

    return version(name)


def _parse_target(value: str) -> tuple[str, str]:
    label, separator, dsn = value.partition("=")
    if not separator or not label or not dsn:
        raise argparse.ArgumentTypeError("target must be LABEL=DSN")
    return label, dsn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        type=_parse_target,
        help="PostgreSQL target as LABEL=DSN; pass at least two",
    )
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    parser.add_argument("--failure-seeds", type=int, default=FAILURE_SEEDS)
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        type=_parse_target,
        help="optional pinned container image as LABEL=REFERENCE",
    )
    parser.add_argument("--output", type=Path, help="write the JSON result to this path")
    args = parser.parse_args()
    if len(args.target) < 2:
        parser.error("EXP-005 requires at least two PostgreSQL targets")
    if args.rounds < 1 or args.failure_seeds < 1:
        parser.error("round counts must be positive")
    document = run_experiment(
        args.target,
        rounds=args.rounds,
        failure_seeds=args.failure_seeds,
        target_images=dict(args.image),
    )
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
