"""Record and replay a planner, three specialists, and a reviewer offline."""

from __future__ import annotations

import json
from typing import Any

from pollard import (
    ActionSpec,
    Budget,
    BudgetExceeded,
    MemoryStore,
    PolicyViolation,
    Registry,
    Runtime,
    Team,
)
from pollard.dependencies import ResultReference, record_dependency, record_handoff
from pollard.team_reports import team_report

FACTS = {"north": 12, "south": 7, "west": 9}


def workflow(store: MemoryStore, *, replay: bool) -> dict[str, Any]:
    dispatches = {"model": 0, "read": 0, "publish": 0}

    def local_model(payload: dict[str, Any]) -> dict[str, Any]:
        assert not replay, "strict replay must not dispatch a model"
        dispatches["model"] += 1
        return {
            "text": str(payload["input"]),
            "usage": {"input_tokens": 2, "output_tokens": 2},
        }

    def read_facts(args: dict[str, Any]) -> dict[str, Any]:
        assert not replay, "strict replay must not dispatch a tool"
        dispatches["read"] += 1
        return {
            "count": FACTS[str(args["region"])],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def publish(_args: dict[str, Any]) -> dict[str, Any]:
        dispatches["publish"] += 1
        raise AssertionError("a restricted agent must never dispatch publish")

    schema = {
        "type": "object",
        "properties": {"region": {"type": "string"}},
        "required": ["region"],
        "additionalProperties": False,
    }
    registry = Registry(
        [
            ActionSpec("read_facts", "1", "Read fixed regional facts.", schema, False, read_facts),
            ActionSpec("publish", "1", "Publish a report.", schema, True, publish),
        ]
    )
    runtime = Runtime(store, registry=registry, mode="replay" if replay else "record")
    team = Team(runtime, "regional-review", budget=Budget(steps=8), allowed_tools=("read_facts",))
    planner = team.agent("planner", task_id="plan", role="planner", budget=Budget(steps=7))
    planner.run.model_call(
        {"model": "local-demo", "input": "Review north, south, and west."}, fn=local_model
    )

    findings = []
    for region in FACTS:
        specialist = planner.delegate(
            f"specialist-{region}",
            task_id=f"inspect-{region}",
            role="researcher",
            budget=Budget(steps=2),
        )
        fact = specialist.run.tool_call("read_facts", {"region": region})
        result = specialist.run.model_call(
            {"model": "local-demo", "input": f"{region}: {fact.result['count']}"},
            fn=local_model,
        )
        findings.append(result)

    reviewer = team.agent(
        "reviewer", task_id="review", role="reviewer", budget=Budget(steps=1), allowed_tools=()
    )
    references = [ResultReference.from_node(node) for node in findings]
    record_handoff(planner.run, references, recipient="reviewer", task_id="review")
    record_dependency(reviewer.run, references, label="regional findings")
    reviewed = reviewer.run.model_call(
        {"model": "local-demo", "input": "; ".join(str(node.result["text"]) for node in findings)},
        fn=local_model,
    )

    try:
        reviewer.run.tool_call("publish", {"region": "all"})
    except PolicyViolation:
        denied_tool = True
    else:
        raise AssertionError("reviewer must be denied publish")

    nested_limit_stopped = False
    if not replay:
        try:
            planner.run.model_call(
                {"model": "local-demo", "input": "One more task."}, fn=local_model
            )
        except BudgetExceeded:
            nested_limit_stopped = True
        else:
            raise AssertionError("planner and its children have used their seven steps")

    report = team_report(store, team.root_id)
    assert report.dependencies.ok
    return {
        "result_id": reviewed.id,
        "text": reviewed.result["text"],
        "denied_tool": denied_tool,
        "nested_limit_stopped": nested_limit_stopped,
        "dispatches": dispatches,
        "report": report.to_dict(),
    }


def main() -> None:
    store = MemoryStore()
    recorded = workflow(store, replay=False)
    replayed = workflow(store, replay=True)
    assert recorded["result_id"] == replayed["result_id"]
    assert replayed["dispatches"] == {"model": 0, "read": 0, "publish": 0}
    print(
        json.dumps(
            {
                "network_used": False,
                "hosted_model_spend_usd": 0,
                "replay_matched": True,
                "recorded": recorded,
                "replayed_dispatches": replayed["dispatches"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
