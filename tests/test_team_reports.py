import json
from dataclasses import replace

from pollard.dependencies import ResultReference, record_dependency, record_handoff
from pollard.meters import StepMeter
from pollard.runtime import Run, Runtime
from pollard.store import MemoryStore
from pollard.team_reports import team_report
from pollard.teams import Team
from pollard.tree import Node, NodeKind


def _call(run: Run, name: str, tokens: int, usd: float, duration: float) -> Node:
    node = run.model_call(
        {"model": name},
        fn=lambda _: {"text": name, "usage": {
            "input_tokens": tokens, "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 1},
        }},
    )
    run.store.update_meta(node.id, {
        "charges": {"tokens": tokens + 2, "usd": usd, "steps": 1}, "duration_s": duration,
    })
    return node


def test_nested_delegates_are_counted_once_and_root_work_is_unattributed() -> None:
    store = MemoryStore()
    runtime = Runtime(store, meters=[StepMeter()])
    team = Team(runtime, "mission")
    common = _call(team.run, "common", 1, 0.1, 1.0)
    planner = team.agent("planner", task_id="plan", role="coordinator")
    plan = _call(planner.run, "plan", 2, 0.2, 2.0)
    child = planner.delegate("researcher", task_id="research", role="reader")
    research = _call(child.run, "research", 3, 0.3, 3.0)
    grandchild = child.delegate("checker", task_id="check", role="reviewer")
    _call(grandchild.run, "check", 4, 0.4, 4.0)
    _call(planner.run, "synthesis", 5, 0.5, 5.0)
    record_handoff(child.run, [ResultReference.from_node(research)], recipient="planner")
    record_dependency(
        planner.run, [ResultReference.from_node(plan), ResultReference.from_node(research)]
    )

    report = team_report(store, team.root_id)
    assert report.ok
    assert report.totals.governed_calls == 5
    assert report.totals.charges == {"tokens": 25, "usd": 1.5, "steps": 5}
    assert report.totals.summed_call_duration_seconds == 15
    assert report.totals.usage == {
        "input_tokens": 15, "output_tokens": 10, "input_tokens_details.cached_tokens": 5,
    }
    by_agent = {agent.agent_id: agent for agent in report.agents}
    assert by_agent["planner"].metrics.governed_calls == 2
    assert by_agent["planner"].metrics.charges["tokens"] == 11
    assert by_agent["researcher"].metrics.charges["tokens"] == 5
    assert by_agent["checker"].metrics.charges["tokens"] == 6
    assert by_agent["researcher"].metrics.handoffs == 1
    assert by_agent["planner"].metrics.dependency_references == 2
    assert report.unattributed_node_ids == [common.id]
    assert report.unattributed.governed_calls == 1
    assert report.orphaned_node_ids == []
    encoded = json.loads(json.dumps(report.to_dict(), allow_nan=False))
    assert encoded["totals"]["model_calls"] == 5
    assert encoded["dependencies"]["ok"] is True
    assert encoded["interpretation"]["identity"] == "caller-declared, not authenticated"


def test_distinct_tasks_and_roles_are_distinct_groups() -> None:
    runtime = Runtime(meters=[StepMeter()])
    team = Team(runtime, "mission")
    first = team.agent("worker", task_id="first", role="researcher")
    second = team.agent("worker", task_id="second", role="reviewer")
    _call(first.run, "one", 1, 0.1, 1.0)
    _call(second.run, "two", 2, 0.2, 2.0)
    report = team_report(runtime.store, team.root_id)
    assert [(item.agent_id, item.task_id, item.role) for item in report.agents] == [
        ("worker", "first", "researcher"), ("worker", "second", "reviewer"),
    ]


def test_refusals_dry_runs_and_post_dispatch_failures_are_distinguished() -> None:
    runtime = Runtime(meters=[StepMeter()])
    team = Team(runtime, "mission")
    worker = team.agent("worker", task_id="work")
    parent = worker.run.cursor_id
    records = [
        Node.make(kind=NodeKind.REFUSAL, parent=parent, payload={"reason": "policy"}),
        Node.make(kind=NodeKind.TOOL_CALL, parent=parent, payload={"tool": "write"},
                  result={"dry_run": True}, meta={"dry_run": True, "charges": {"steps": 1}}),
        Node.make(kind=NodeKind.NOTE, parent=parent,
                  payload={"event": "call_outcome_unknown", "blocked_kind": "model_call"},
                  meta={"duration_s": 2.5, "charges": {"tokens": 20}}),
    ]
    for node in records:
        runtime.store.put(node)
    report = team_report(runtime.store, team.root_id)
    assert report.totals.governed_calls == 2
    assert report.totals.model_calls == 1
    assert report.totals.tool_calls == 1
    assert report.totals.refusals == 1
    assert report.totals.dry_runs == 1
    assert report.totals.failed_outcomes == 1
    assert report.totals.charges == {"steps": 1, "tokens": 20}
    assert report.totals.summed_call_duration_seconds == 2.5


def test_malformed_actor_does_not_misattribute_descendants_to_parent() -> None:
    runtime = Runtime(meters=[StepMeter()])
    team = Team(runtime, "mission")
    parent = team.agent("parent", task_id="parent")
    _call(parent.run, "parent", 1, 0.1, 1.0)
    parent.run.note({"_pollard": {"team_agent": {"version": 1, "identity": {"agent_id": "bad"}}}})
    orphan = Node.make(
        kind=NodeKind.MODEL_CALL, parent=parent.run.cursor_id, payload={"model": "orphan"},
        result={"text": "orphan"}, meta={"charges": {"tokens": 4}},
    )
    runtime.store.put(orphan)
    report = team_report(runtime.store, team.root_id)
    assert not report.ok
    assert report.findings[0].code == "invalid_actor"
    assert report.orphaned_node_ids == [orphan.id]
    assert report.unattributed.charges["tokens"] == 4
    assert report.agents[0].metrics.governed_calls == 1


def test_tampered_actor_is_reported_instead_of_trusting_declared_identity() -> None:
    store = MemoryStore()
    runtime = Runtime(store, meters=[StepMeter()])
    team = Team(runtime, "mission")
    worker = team.agent("worker", task_id="task")
    anchor = store.get(worker.run.cursor_id)
    call = _call(worker.run, "work", 2, 0.2, 2.0)
    store._nodes[anchor.id] = replace(anchor, payload={**anchor.payload, "tampered": True})
    report = team_report(store, team.root_id)
    assert not report.ok
    assert report.agents == []
    assert report.orphaned_node_ids == [call.id]


def test_invalid_metadata_is_ignored_and_usage_fallback_is_not_double_counted() -> None:
    runtime = Runtime(meters=[StepMeter()])
    team = Team(runtime, "mission")
    worker = team.agent("worker", task_id="task")
    node = worker.run.model_call({}, fn=lambda _: {"usage": {"input_tokens": 4}})
    runtime.store.update_meta(node.id, {
        "charges": {"bad": True, "negative": -1, "tokens": 4},
        "usage": {"input_tokens": 4, "invalid": "no", "negative": -1}, "duration_s": -1,
    })
    report = team_report(runtime.store, team.root_id)
    assert report.totals.charges == {"tokens": 4}
    assert report.totals.usage == {"input_tokens": 4}
    assert report.totals.summed_call_duration_seconds == 0


def test_replay_does_not_inflate_recorded_accounting() -> None:
    store = MemoryStore()
    runtime = Runtime(store, meters=[StepMeter()])
    team = Team(runtime, "mission")
    worker = team.agent("worker", task_id="task")
    _call(worker.run, "work", 2, 0.2, 2.0)
    before = team_report(store, team.root_id).to_dict()
    replay = Team(Runtime(store, mode="replay", meters=[StepMeter()]), "mission")
    repeated = replay.agent("worker", task_id="task")
    repeated.run.model_call({"model": "work"}, fn=lambda _: {"incorrect": "would execute"})
    assert team_report(store, team.root_id).to_dict() == before


def test_missing_root_returns_a_json_friendly_failure_report() -> None:
    report = team_report(MemoryStore(), "0" * 64)
    assert not report.ok
    assert report.totals.governed_calls == 0
    assert report.dependencies.findings[0].code == "invalid_root"
    json.dumps(report.to_dict(), allow_nan=False)
