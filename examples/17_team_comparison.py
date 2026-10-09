"""Compare one and three workers on a fixed local task with synthetic usage."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any

from pollard import Budget, MemoryStore, Runtime, Team
from pollard.team_reports import team_report

ROWS = [{"id": f"case-{index:02d}", "needs_review": index % 2 == 0} for index in range(1, 7)]
EXPECTED = ["case-02", "case-04", "case-06"]


def compare(workers: int) -> dict[str, Any]:
    started = perf_counter()
    store = MemoryStore()
    team = Team(Runtime(store), f"comparison-{workers}", budget=Budget(steps=workers))
    jobs = [
        (
            team.agent(f"worker-{index}", task_id=f"partition-{index}", budget=Budget(steps=1)),
            ROWS[index::workers],
        )
        for index in range(workers)
    ]

    def local_model(payload: dict[str, Any]) -> dict[str, Any]:
        rows = payload["rows"]
        findings = [row["id"] for row in rows if row["needs_review"]]
        return {
            "findings": findings,
            "usage": {"input_tokens": 5 + 2 * len(rows), "output_tokens": 2 * len(findings)},
        }

    def execute(job: Any) -> list[str]:
        agent, rows = job
        node = agent.run.model_call(
            {"model": "deterministic-selector", "rows": rows}, fn=local_model
        )
        return list(node.result["findings"])

    with ThreadPoolExecutor(max_workers=workers) as executor:
        findings = sorted(item for result in executor.map(execute, jobs) for item in result)
    report = team_report(store, team.root_id)
    elapsed = perf_counter() - started
    return {
        "workers": workers,
        "quality": {
            "expected_findings": len(EXPECTED),
            "correct_findings": len(set(findings) & set(EXPECTED)),
            "exact_match": findings == EXPECTED,
        },
        "findings": findings,
        "local_elapsed_seconds": elapsed,
        "report": report.to_dict(),
    }


def main() -> None:
    results = [compare(1), compare(3)]
    assert all(row["quality"]["exact_match"] for row in results)
    print(
        json.dumps(
            {
                "network_used": False,
                "hosted_model_spend_usd": 0,
                "usage_source": "Synthetic counters: five input units per call plus two per row.",
                "timing_scope": "Local Python execution, including setup and reporting.",
                "interpretation": (
                    "Fixed task correctness and accounting only; no model quality or speed claim."
                ),
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
