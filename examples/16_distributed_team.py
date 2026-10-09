"""Attach four independent SQLite workers to a team with three exact steps."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from typing import Any

from pollard import Budget, BudgetExceeded, Runtime, SQLiteStore, Team
from pollard.team_reports import team_report


def run_demo(path: Path) -> dict[str, Any]:
    with SQLiteStore(path) as store:
        coordinator = Team(Runtime(store), "sqlite-team", budget=Budget(steps=3))
        root_id = coordinator.root_id
        contexts = [
            json.loads(
                json.dumps(
                    coordinator.agent(
                        f"worker-{index}",
                        task_id=f"task-{index}",
                        budget=Budget(steps=1),
                    )
                    .context()
                    .to_dict()
                )
            )
            for index in range(4)
        ]

    start = Barrier(4)
    lock = Lock()
    executed = 0

    def worker(context: dict[str, Any]) -> dict[str, Any]:
        nonlocal executed

        def local_model(_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal executed
            with lock:
                executed += 1
            return {"text": "finished", "usage": {"input_tokens": 1, "output_tokens": 1}}

        with SQLiteStore(path) as store:
            team = Team(Runtime(store), "sqlite-team", budget=Budget(steps=3))
            agent = team.attach(context)
            start_cursor = agent.run.cursor_id
            start.wait(timeout=30)
            try:
                agent.run.model_call(
                    {"model": "local-demo", "input": "Finish the assigned task."}, fn=local_model
                )
                accepted = True
            except BudgetExceeded:
                accepted = False
            checkpoint = json.loads(json.dumps(agent.checkpoint().to_dict()))
            restored = team.restore(checkpoint)
            assert restored.run.cursor_id == agent.run.cursor_id
            return {"accepted": accepted, "start_cursor": start_cursor, "checkpoint_restored": True}

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(worker, contexts))
    with SQLiteStore(path) as store:
        report = team_report(store, root_id)

    accepted = sum(row["accepted"] for row in results)
    assert accepted == executed == 3
    assert len({row["start_cursor"] for row in results}) == 4
    return {
        "network_used": False,
        "hosted_model_spend_usd": 0,
        "attached_agents": 4,
        "accepted": accepted,
        "refused": 4 - accepted,
        "callable_executions": executed,
        "unique_start_cursors": 4,
        "checkpoints_restored": all(row["checkpoint_restored"] for row in results),
        "report": report.to_dict(),
    }


def main() -> None:
    with TemporaryDirectory(prefix="pollard-team-") as directory:
        result = run_demo(Path(directory) / "team.db")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
