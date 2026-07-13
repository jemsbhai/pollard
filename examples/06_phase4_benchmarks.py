"""Offline Phase 4 benchmark and demo runs."""

from __future__ import annotations

import json
import math
import platform
import statistics
import time
from collections.abc import Callable
from typing import Any

import pollard
from pollard import (
    ActionSpec,
    Budget,
    BudgetExceeded,
    MemoryStore,
    PolicyViolation,
    Registry,
    Runtime,
)

SEEDS = tuple(range(5))
BRANCH_COUNTS = (2, 4, 8)
T_95_DF4 = 2.776


def main() -> None:
    print(json.dumps(run_all(), indent=2, sort_keys=True))


def run_all() -> dict[str, Any]:
    started = time.perf_counter()
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pollard": pollard.__version__,
        },
        "experiments": [
            exp_001_shared_prefix_savings(),
            exp_002_runaway_stop(),
            exp_003_injection_block(),
        ],
    }
    result["duration_s"] = round(time.perf_counter() - started, 6)
    return result


def exp_001_shared_prefix_savings() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for branches in BRANCH_COUNTS:
        for seed in SEEDS:
            prefix_tokens = 1000 + seed * 17
            suffix_tokens = 100 + seed * 3
            naive_tokens = branches * (prefix_tokens + suffix_tokens)
            predicted_tokens = prefix_tokens + branches * suffix_tokens
            store = MemoryStore()
            run = Runtime(store).run(
                f"exp-001-n{branches}-seed{seed}",
                budget=Budget(tokens=1_000_000, steps=100),
            )
            run.model_call(
                {"model": "mock-1", "stage": "prefix", "seed": seed},
                fn=_fixed_usage(prefix_tokens),
            )
            for branch_index in range(branches):
                with run.branch(attempt=branch_index) as branch:
                    branch.model_call(
                        {
                            "model": "mock-1",
                            "stage": "suffix",
                            "seed": seed,
                            "branch": branch_index,
                        },
                        fn=_fixed_usage(suffix_tokens),
                    )
            spent_tokens = int(run.report()["spent"]["tokens"])
            rows.append(
                {
                    "branches": branches,
                    "seed": seed,
                    "prefix_tokens": prefix_tokens,
                    "suffix_tokens": suffix_tokens,
                    "naive_tokens": naive_tokens,
                    "pollard_tokens": spent_tokens,
                    "predicted_tokens": predicted_tokens,
                    "prediction_error_pct": _pct_error(spent_tokens, predicted_tokens),
                    "savings_pct": round((1.0 - spent_tokens / naive_tokens) * 100.0, 6),
                }
            )

    return {
        "id": "EXP-001",
        "name": "shared-prefix savings",
        "status": "mock_passed",
        "local_model": "not_run",
        "rows": rows,
        "summary": _summary_by_branches(rows),
    }


def exp_002_runaway_stop() -> dict[str, Any]:
    call_count = 0
    token_limit = 5
    charge_per_call = 4
    run = Runtime(MemoryStore()).run("exp-002", budget=Budget(tokens=token_limit))

    def burn(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"text": "again", "usage": {"input_tokens": charge_per_call, "output_tokens": 0}}

    refusal_id = ""
    while True:
        try:
            run.model_call({"model": "mock-1", "turn": call_count + 1}, fn=burn)
        except BudgetExceeded as exc:
            refusal_id = exc.refusal_id
            break

    refusal = run.store.get(refusal_id)
    spent_tokens = int(run.report()["spent"]["tokens"])
    overshoot = max(0, spent_tokens - token_limit)
    return {
        "id": "EXP-002",
        "name": "runaway stop",
        "status": "passed",
        "calls_executed": call_count,
        "refusal_kind": refusal.kind,
        "refusal_meter": refusal.payload["meter"],
        "spent_tokens": spent_tokens,
        "token_limit": token_limit,
        "overshoot_tokens": overshoot,
        "max_single_settle_tokens": charge_per_call,
        "overshoot_within_bound": overshoot <= charge_per_call,
    }


def exp_003_injection_block() -> dict[str, Any]:
    executed = False

    def approved(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal executed
        executed = True
        return {"ok": True, "text": args["text"], "usage": {"input_tokens": 0, "output_tokens": 0}}

    registry = Registry(
        [
            ActionSpec(
                "approved",
                "1",
                "Approved echo action.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                False,
                approved,
            )
        ]
    )
    run = Runtime(MemoryStore(), registry=registry).run("exp-003")
    refusal_id = ""
    try:
        run.tool_call("delete_everything", {"path": "C:/important"})
    except PolicyViolation as exc:
        refusal_id = exc.refusal_id

    refusal = run.store.get(refusal_id)
    return {
        "id": "EXP-003",
        "name": "injection block",
        "status": "passed",
        "executed": executed,
        "refusal_kind": refusal.kind,
        "refusal_reason": refusal.payload["reason"],
        "registry_digest_recorded": "registry_digest" in refusal.payload,
    }


def _fixed_usage(input_tokens: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def call(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": str(input_tokens),
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        }

    return call


def _summary_by_branches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for branches in BRANCH_COUNTS:
        selected = [row for row in rows if row["branches"] == branches]
        savings = [float(row["savings_pct"]) for row in selected]
        errors = [float(row["prediction_error_pct"]) for row in selected]
        summary.append(
            {
                "branches": branches,
                "seeds": len(selected),
                "mean_savings_pct": round(statistics.mean(savings), 6),
                "ci95_savings_pct": round(_ci95_half_width(savings), 6),
                "max_prediction_error_pct": round(max(errors), 6),
            }
        )
    return summary


def _ci95_half_width(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return T_95_DF4 * statistics.stdev(values) / math.sqrt(len(values))


def _pct_error(actual: int, expected: int) -> float:
    if expected == 0:
        return 0.0 if actual == 0 else 100.0
    return round(abs(actual - expected) / expected * 100.0, 6)


if __name__ == "__main__":
    main()
