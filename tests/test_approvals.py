import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from pollard.aio import AsyncRun, AsyncRuntime
from pollard.approvals import ApprovalPolicy, ApprovalRequest, decide_approval, request_approval
from pollard.errors import ConfirmationRequired, IntegrityError, PolicyViolation
from pollard.meters import StepMeter
from pollard.policy import Decision, PolicyContext
from pollard.registry import ActionSpec, Registry
from pollard.runtime import Run, Runtime
from pollard.store import MemoryStore, Store
from pollard.stores.sqlite import SQLiteStore
from pollard.teams import Team
from pollard.tree import Node, NodeKind


def _registry(calls: list[str]) -> Registry:
    def write(args: dict[str, object]) -> dict[str, object]:
        calls.append(str(args["secret"]))
        return {"saved": True}

    return Registry([ActionSpec(
        "write", "1", "write a secret", {"type": "object", "properties": {
            "secret": {"type": "string", "sensitive": True},
        }, "required": ["secret"], "additionalProperties": False},
        side_effects=True, handler=write,
    )])


def _runtime(store: Store, calls: list[str], mode: str = "record") -> Runtime:
    return Runtime(
        store, registry=_registry(calls), policies=[ApprovalPolicy(store)],
        meters=[StepMeter()], mode=mode,
    )


def test_approval_survives_checkpoint_reopen_and_executes_exact_action(tmp_path: Path) -> None:
    calls: list[str] = []
    path = tmp_path / "approvals.db"
    with SQLiteStore(path) as store:
        team = Team(_runtime(store, calls), "mission")
        worker = team.agent("worker", task_id="write", allowed_tools=("write",))
        request = request_approval(worker.run, "write", {"secret": "private value"})
        checkpoint = worker.checkpoint()
        encoded = request.to_dict()
        assert "private value" not in json.dumps(encoded)
        recorded_payloads = [node.payload for node in store.walk(team.root_id)]
        assert "private value" not in json.dumps(recorded_payloads)
    with SQLiteStore(path) as store:
        transported = ApprovalRequest.from_dict(json.loads(json.dumps(encoded)))
        assert transported == request
        decision = decide_approval(store, transported, approved=True, reviewer="operator")
        assert decide_approval(store, transported, approved=True, reviewer="operator") == decision
    with SQLiteStore(path) as store:
        team = Team(_runtime(store, calls), "mission")
        worker = team.restore(checkpoint)
        assert worker.run.tool_call("write", {"secret": "private value"}).result == {"saved": True}
        with pytest.raises(PolicyViolation):
            worker.run.tool_call("write", {"secret": "private value"})
    assert calls == ["private value"]


def test_missing_and_denied_approval_never_dispatch() -> None:
    calls: list[str] = []
    store = MemoryStore()
    runtime = _runtime(store, calls)
    run = runtime.run("mission")
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "value"})
    request = request_approval(run, "write", {"secret": "value"})
    decide_approval(store, request, approved=False, reviewer="operator")
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "value"})
    assert calls == []


@pytest.mark.parametrize("state", ["missing", "exhausted"])
def test_durable_rejection_cannot_be_confirmed_with_an_in_memory_token(state: str) -> None:
    calls: list[str] = []
    store = MemoryStore()
    run = _runtime(store, calls).run("mission")
    if state == "exhausted":
        request = request_approval(run, "write", {"secret": "one"})
        decide_approval(store, request, approved=True, reviewer="operator")
        run.tool_call("write", {"secret": "one"})
    with pytest.raises(PolicyViolation) as caught:
        run.tool_call("write", {"secret": "one"})
    with pytest.raises(KeyError):
        run.confirm(caught.value.refusal_id)
    assert run._pending_tool_calls == {}
    assert calls == (["one"] if state == "exhausted" else [])


class _ConfirmEveryAction:
    def decide(self, ctx: PolicyContext) -> Decision:
        return Decision.CONFIRM


def _invoke_tool(run: Run) -> Node:
    if isinstance(run, AsyncRun):
        return asyncio.run(run.atool_call("write", {"secret": "one"}))
    return run.tool_call("write", {"secret": "one"})


def _confirm_tool(run: Run, token: str) -> Node:
    if isinstance(run, AsyncRun):
        return asyncio.run(run.aconfirm(token))
    return run.confirm(token)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_durable_denial_precedes_an_earlier_unrelated_confirmation_policy(
    asynchronous: bool,
) -> None:
    calls: list[str] = []
    store = MemoryStore()
    runtime_type = AsyncRuntime if asynchronous else Runtime
    runtime = runtime_type(
        store, registry=_registry(calls), meters=[StepMeter()],
        policies=[_ConfirmEveryAction(), ApprovalPolicy(store)],
    )
    run = runtime.run("mission")
    with pytest.raises(PolicyViolation):
        _invoke_tool(run)
    assert run._pending_tool_calls == {}
    assert calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stale_confirmation_token_rechecks_durable_approval(asynchronous: bool) -> None:
    calls: list[str] = []
    store = MemoryStore()
    runtime_type = AsyncRuntime if asynchronous else Runtime
    runtime = runtime_type(
        store, registry=_registry(calls), meters=[StepMeter()],
        policies=[_ConfirmEveryAction(), ApprovalPolicy(store)],
    )
    run = runtime.run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    with pytest.raises(ConfirmationRequired) as caught:
        _invoke_tool(run)
    other = Runtime(store, registry=_registry(calls), meters=[StepMeter()]).run("mission")
    other.cursor_id = request.id
    other.model_call({}, fn=lambda _: {"text": "intervening work"})
    with pytest.raises(PolicyViolation):
        _confirm_tool(run, caught.value.resume_token)
    assert calls == []


def test_args_actor_registry_and_spec_changes_require_new_approval() -> None:
    calls: list[str] = []
    store = MemoryStore()
    runtime = _runtime(store, calls)
    team = Team(runtime, "mission")
    worker = team.agent("worker", task_id="work")
    request = request_approval(worker.run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    assert runtime.registry is not None
    context = PolicyContext(
        runtime.registry.get("write"), {"secret": "one"}, worker.run.cursor_id,
        "mission", {}, worker.identity, runtime.registry.registry_digest,
    )
    policy = ApprovalPolicy(store)
    assert policy.decide(context) == Decision.ALLOW
    assert policy.decide(replace(context, args={"secret": "two"})) == Decision.DENY
    assert policy.decide(replace(context, agent_identity=None)) == Decision.DENY
    assert policy.decide(replace(context, registry_digest="0" * 64)) == Decision.DENY
    assert policy.decide(replace(context, registry_digest=None)) == Decision.DENY
    changed = ActionSpec("write", "2", "changed", context.spec.schema, side_effects=True)
    assert policy.decide(replace(context, spec=changed)) == Decision.DENY
    assert calls == []


@pytest.mark.parametrize("intervening", ["model", "tool", "failed"])
def test_approval_is_not_reusable_after_call_or_rollback(intervening: str) -> None:
    calls: list[str] = []
    store = MemoryStore()
    runtime = _runtime(store, calls)
    run = runtime.run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    if intervening == "model":
        run.model_call({}, fn=lambda _: {"text": "intervened"})
    elif intervening == "tool":
        run.tool_call("write", {"secret": "one"})
    else:
        run.note({"event": "call_outcome_unknown", "blocked_kind": "tool_call"})
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "one"})
    run.rollback(request.id)
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "one"})
    assert len(calls) == (1 if intervening == "tool" else 0)


def test_replay_request_and_action_do_not_write_or_dispatch() -> None:
    calls: list[str] = []
    store = MemoryStore()
    run = _runtime(store, calls).run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    expected = run.tool_call("write", {"secret": "one"})
    before = dict(store._nodes)
    replay = _runtime(store, calls, "replay").run("mission")
    assert request_approval(replay, "write", {"secret": "one"}) == request
    assert replay.tool_call("write", {"secret": "one"}).id == expected.id
    assert store._nodes == before
    assert calls == ["one"]


def test_concurrent_conflicting_decisions_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "decisions.db"
    with SQLiteStore(path) as store:
        request = request_approval(_runtime(store, []).run("mission"), "write", {"secret": "one"})
    barrier = Barrier(2)

    def decide(approved: bool) -> bool:
        with SQLiteStore(path) as store:
            barrier.wait()
            try:
                decide_approval(store, request, approved=approved, reviewer="operator")
            except IntegrityError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [True, False]))
    assert sorted(results) == [False, True]


def test_tampered_and_missing_requests_are_rejected_before_any_write() -> None:
    store = MemoryStore()
    run = _runtime(store, []).run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    with pytest.raises(IntegrityError, match="ID"):
        ApprovalRequest.from_dict({**request.to_dict(), "args_digest": "0" * 64})
    empty = MemoryStore()
    with pytest.raises(IntegrityError):
        decide_approval(empty, request, approved=True, reviewer="operator")
    assert empty._nodes == {}
    node = store.get(request.id)
    store._nodes[node.id] = replace(node, payload={**node.payload, "tampered": True})
    count = len(store._nodes)
    with pytest.raises(IntegrityError):
        decide_approval(store, request, approved=True, reviewer="operator")
    assert len(store._nodes) == count


def test_interrupted_decision_creation_can_be_repaired_and_policy_fails_closed() -> None:
    store = MemoryStore()
    runtime = _runtime(store, [])
    run = runtime.run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decision = decide_approval(store, request, approved=True, reviewer="operator")
    store._pollard_drop_nodes({decision.id})
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "one"})
    assert decide_approval(store, request, approved=True, reviewer="operator").id == decision.id
    assert run.tool_call("write", {"secret": "one"}).result == {"saved": True}


def test_bad_arguments_missing_registry_and_invalid_decision_do_not_add_nodes() -> None:
    plain = Runtime(meters=[StepMeter()]).run("plain")
    with pytest.raises(ValueError, match="registry"):
        request_approval(plain, "write", {})
    store = MemoryStore()
    run = _runtime(store, []).run("mission")
    with pytest.raises(ValueError, match="arguments"):
        request_approval(run, "write", {})
    request = request_approval(run, "write", {"secret": "one"})
    count = len(store._nodes)
    with pytest.raises(TypeError, match="boolean"):
        decide_approval(store, request, approved="yes", reviewer="operator")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reviewer"):
        decide_approval(store, request, approved=True, reviewer="")
    assert len(store._nodes) == count


def test_pure_read_policy_and_optional_readonly_action_exemption() -> None:
    store = MemoryStore()
    run = _runtime(store, []).run("mission")
    readonly = ActionSpec("read", "1", "read", {"type": "object"}, side_effects=False)
    context = PolicyContext(readonly, {}, run.cursor_id, "mission", {})
    assert ApprovalPolicy(store).decide(context) == Decision.ALLOW
    assert ApprovalPolicy(store, side_effects_only=False).decide(context) == Decision.DENY
    config = ApprovalPolicy(store).pollard_team_config()
    assert config == {"version": 1, "side_effects_only": True}
    assert "store" not in json.dumps(config)


def test_tampered_decision_never_authorizes_a_call() -> None:
    store = MemoryStore()
    run = _runtime(store, []).run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decision = decide_approval(store, request, approved=True, reviewer="operator")
    store._nodes[decision.id] = replace(decision, payload={"tampered": True})
    with pytest.raises(IntegrityError):
        run.tool_call("write", {"secret": "one"})


def test_cross_run_request_is_not_applicable() -> None:
    store = MemoryStore()
    runtime = _runtime(store, [])
    run = runtime.run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    other = runtime.run("another-mission")
    with pytest.raises(PolicyViolation):
        other.tool_call("write", {"secret": "one"})


def test_resultless_call_still_exhausts_approval() -> None:
    store = MemoryStore()
    run = _runtime(store, []).run("mission")
    request = request_approval(run, "write", {"secret": "one"})
    decide_approval(store, request, approved=True, reviewer="operator")
    store.put(Node.make(kind=NodeKind.TOOL_CALL, parent=request.id, payload={"dry_run": True}))
    with pytest.raises(PolicyViolation):
        run.tool_call("write", {"secret": "one"})
