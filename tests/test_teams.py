import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier, Lock

import pytest

from pollard.aio import AsyncRun, AsyncRuntime
from pollard.errors import BudgetExceeded, IntegrityError, PolicyViolation
from pollard.governor import Budget
from pollard.meters import StepMeter, TokenMeter, WindowMeter
from pollard.policy import Decision, PolicyContext
from pollard.registry import ActionSpec, Registry
from pollard.runtime import Runtime
from pollard.scopes import SharedBudget
from pollard.store import MemoryStore
from pollard.stores import SQLiteStore
from pollard.team_context import AgentCheckpoint, AgentIdentity, DelegationContext
from pollard.teams import Team


def registry() -> Registry:
    return Registry(
        [
            ActionSpec(
                name=name,
                version="1",
                description=name,
                schema={"type": "object"},
                side_effects=False,
                handler=lambda _args: {"ok": True},
            )
            for name in ("read", "write")
        ]
    )


def test_agent_identity_validates_and_freezes_capabilities() -> None:
    identity = AgentIdentity("team", "researcher", "task", allowed_tools=("write", "read"))
    assert identity.allowed_tools == ("read", "write")
    assert AgentIdentity.from_dict(identity.to_dict()) == identity
    with pytest.raises(FrozenInstanceError):
        identity.agent_id = "other"  # type: ignore[misc]
    for name in ("", " bad", "bad\nname"):
        with pytest.raises(ValueError):
            AgentIdentity("team", name, "task")
    with pytest.raises(TypeError):
        AgentIdentity("team", "agent", "task", allowed_tools=["read"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AgentIdentity("team", "agent", "task", allowed_tools=("read", "read"))


def test_team_agents_have_independent_stable_cursors() -> None:
    runtime = Runtime(meters=[StepMeter()])
    team = Team(runtime, "mission", budget=Budget(steps=4))
    first = team.agent("researcher", task_id="research")
    second = team.agent("reviewer", task_id="review")
    second_cursor = second.run.cursor_id
    node = first.run.model_call({"model": "mock"}, fn=lambda _: {"text": "result"})
    assert first.run.cursor_id == node.id
    assert second.run.cursor_id == second_cursor
    assert team.run.cursor_id == team.manifest_id
    assert first.run.agent_identity == first.identity
    assert first.run.root_id == second.run.root_id == team.root_id
    assert (
        Team(runtime, "mission", budget=Budget(steps=4))
        .agent("researcher", task_id="research")
        .delegation_id
        == first.delegation_id
    )


def test_nested_delegation_retains_budgets_and_attenuates_tools() -> None:
    team = Team(
        Runtime(meters=[StepMeter()], registry=registry()),
        "mission",
        budget=Budget(steps=5),
        allowed_tools=("read", "write"),
    )
    parent = team.agent("lead", task_id="plan", budget=Budget(steps=2), allowed_tools=("read",))
    child = parent.delegate("worker", task_id="read", budget=Budget(steps=1))
    assert child.identity.allowed_tools == ("read",)
    assert len(child.context().scopes) == 3
    child.run.tool_call("read", {})
    with pytest.raises(BudgetExceeded):
        child.run.tool_call("read", {}, attempt=1)
    parent.run.tool_call("read", {})
    with pytest.raises(BudgetExceeded):
        parent.run.tool_call("read", {}, attempt=1)
    with pytest.raises(ValueError, match="widen"):
        parent.delegate("writer", task_id="write", allowed_tools=("write",))


def test_disallowed_tools_are_refused_before_handler() -> None:
    team = Team(Runtime(registry=registry(), meters=[StepMeter()]), "restricted")
    agent = team.agent("reader", task_id="read", allowed_tools=("read",))
    with pytest.raises(PolicyViolation):
        agent.run.tool_call("write", {})
    with agent.run.branch() as branch, pytest.raises(PolicyViolation):
        branch.tool_call("write", {})
    with pytest.raises(ValueError, match="registry"):
        Team(Runtime(), "no-registry", allowed_tools=())


@pytest.mark.parametrize("changed", ["budget", "role", "capabilities"])
def test_conflicting_agent_identity_reuse_is_rejected(changed: str) -> None:
    team = Team(Runtime(registry=registry()), "stable")
    team.agent(
        "agent", task_id="task", role="reader", budget=Budget(steps=2), allowed_tools=("read",)
    )
    with pytest.raises(IntegrityError, match="conflicting reuse"):
        team.agent(
            "agent",
            task_id="task",
            role="writer" if changed == "role" else "reader",
            budget=Budget(steps=3 if changed == "budget" else 2),
            allowed_tools=("read", "write") if changed == "capabilities" else ("read",),
        )


def test_conflicting_team_configuration_is_rejected() -> None:
    store = MemoryStore()
    Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=3))
    with pytest.raises(IntegrityError, match="conflicting reuse"):
        Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=4))
    with pytest.raises(IntegrityError, match="conflicting reuse"):
        Team(
            Runtime(store, meters=[WindowMeter("requests", 4, 60)]),
            "mission",
            budget=Budget(steps=3),
        )


def test_transport_roundtrip_retains_exact_worker_and_nested_scopes(tmp_path: Path) -> None:
    path = tmp_path / "team.db"
    with SQLiteStore(path) as first_store:
        team = Team(Runtime(first_store, meters=[StepMeter()]), "mission", budget=Budget(steps=4))
        parent = team.agent("lead", task_id="plan", budget=Budget(steps=2))
        child = parent.delegate("reader", task_id="read", budget=Budget(steps=1))
        child.run.model_call({"model": "mock"}, fn=lambda _: {"text": "done"})
        document = json.loads(json.dumps(child.context().to_dict()))
    with SQLiteStore(path) as second_store:
        team = Team(Runtime(second_store, meters=[StepMeter()]), "mission", budget=Budget(steps=4))
        attached = team.attach(DelegationContext.from_dict(document))
        assert attached.run.cursor_id == document["cursor_id"]
        assert len(attached.context().scopes) == 3
        with pytest.raises(BudgetExceeded):
            attached.run.model_call({"model": "mock", "next": True}, fn=lambda _: {})


@pytest.mark.parametrize("changed", ["identity", "budget", "scope_removal", "cursor", "config"])
def test_attach_rejects_tampered_transport(changed: str) -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission", budget=Budget(steps=4))
    agent = team.agent("worker", task_id="work", budget=Budget(steps=2))
    context = agent.context()
    if changed == "identity":
        context = replace(context, identity=replace(context.identity, agent_id="another"))
    elif changed == "budget":
        context = replace(
            context,
            scopes=(*context.scopes[:-1], replace(context.scopes[-1], limits=(("steps", "200"),))),
        )
    elif changed == "scope_removal":
        context = replace(context, scopes=context.scopes[1:])
    elif changed == "cursor":
        context = replace(context, cursor_id=team.agent("other", task_id="other").run.cursor_id)
    else:
        context = replace(context, config_digest="a" * 64)
    with pytest.raises(IntegrityError):
        team.attach(context)


def test_attach_rejects_runtime_meter_drift() -> None:
    meter = TokenMeter(reserved_output_tokens=10)
    runtime = Runtime(meters=[meter])
    team = Team(runtime, "mission")
    context = team.agent("worker", task_id="work").context()
    meter._reserved_output_tokens = 20
    with pytest.raises(IntegrityError, match="configuration"):
        team.attach(context)


def test_checkpoint_restores_exact_cursor_not_deepest_leaf(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.db"
    with SQLiteStore(path) as store:
        team = Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=10))
        agent = team.agent("first", task_id="first", budget=Budget(steps=2))
        first = agent.run.model_call({"n": 1}, fn=lambda _: {"ok": True})
        checkpoint = AgentCheckpoint.from_dict(json.loads(json.dumps(agent.checkpoint().to_dict())))
        assert agent.run.cursor_id == first.id
        other = team.agent("other", task_id="other")
        for index in range(4):
            other.run.model_call({"n": index}, fn=lambda _: {"ok": True})
    with SQLiteStore(path) as store:
        team = Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=10))
        restored = team.restore(checkpoint)
        assert restored.run.cursor_id == first.id
        restored.run.model_call({"n": 2}, fn=lambda _: {"ok": True})
        with pytest.raises(BudgetExceeded):
            restored.run.model_call({"n": 3}, fn=lambda _: {})
        with pytest.raises(IntegrityError):
            team.restore(
                replace(
                    checkpoint, context=replace(checkpoint.context, cursor_id=other.run.cursor_id)
                )
            )


def test_team_replay_reuses_structure_and_results_without_dispatch() -> None:
    store = MemoryStore()
    team = Team(Runtime(store, meters=[StepMeter()]), "replay", budget=Budget(steps=2))
    agent = team.agent("worker", task_id="work", budget=Budget(steps=1))
    recorded = agent.run.model_call({"input": "hi"}, fn=lambda _: {"text": "hello"})
    replay = Team(
        Runtime(store, mode="replay", meters=[StepMeter()]), "replay", budget=Budget(steps=2)
    )
    worker = replay.agent("worker", task_id="work", budget=Budget(steps=1))

    def forbidden(_payload: object) -> dict[str, object]:
        raise AssertionError("replay dispatched a callable")

    assert worker.run.model_call({"input": "hi"}, fn=forbidden).id == recorded.id


def test_async_workers_keep_independent_ancestry() -> None:
    async def scenario() -> None:
        team = Team(AsyncRuntime(meters=[StepMeter()]), "async")
        first = team.agent("first", task_id="first")
        second = team.agent("second", task_id="second")
        assert isinstance(first.run, AsyncRun)
        first_parent = first.run.cursor_id
        second_parent = second.run.cursor_id
        entered = 0
        ready = asyncio.Event()

        async def result(_payload: object) -> dict[str, str]:
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return {"text": "done"}

        nodes = await asyncio.gather(
            first.run.amodel_call({"input": "same"}, fn=result),
            second.run.amodel_call({"input": "same"}, fn=result),
        )
        assert nodes[0].parent == first_parent
        assert nodes[1].parent == second_parent
        assert isinstance(team.attach(first.context()).run, AsyncRun)

    asyncio.run(scenario())


def test_policy_receives_delegated_identity() -> None:
    class IdentityPolicy:
        def pollard_team_config(self) -> dict[str, str]:
            return {"version": "1"}

        def decide(self, ctx: PolicyContext) -> Decision:
            assert ctx.agent_identity is not None
            return Decision.ALLOW if ctx.agent_identity.role == "reader" else Decision.DENY

    team = Team(
        Runtime(registry=registry(), policies=[IdentityPolicy()], meters=[StepMeter()]), "policy"
    )
    team.agent("reader", task_id="read", role="reader").run.tool_call("read", {})
    with pytest.raises(PolicyViolation):
        team.agent("writer", task_id="write", role="writer").run.tool_call("read", {})


def test_concurrent_team_workers_share_one_exact_budget(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    with SQLiteStore(path) as store:
        team = Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=3))
        contexts = [
            team.agent(str(index), task_id="work").context().to_dict() for index in range(2)
        ]
    start = Barrier(2)
    lock = Lock()
    executed: list[str] = []

    def worker(index: int) -> None:
        with SQLiteStore(path) as store:
            team = Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=3))
            agent = team.attach(contexts[index])
            start.wait(timeout=10)
            for attempt in range(4):

                def call(_payload: object) -> dict[str, bool]:
                    with lock:
                        executed.append(agent.identity.agent_id)
                    return {"ok": True}

                try:
                    agent.run.model_call({"attempt": attempt}, fn=call)
                except BudgetExceeded:
                    return

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, range(2)))
    assert len(executed) == 3


def test_named_scope_survives_transport_and_cannot_be_omitted(tmp_path: Path) -> None:
    path = tmp_path / "organization.db"
    organization = SharedBudget("organization", Budget(steps=2))
    with SQLiteStore(path) as store:
        runtime = Runtime(store, meters=[StepMeter()], shared_budgets=[organization])
        team = Team(runtime, "mission", budget=Budget(steps=4))
        agent = team.agent("worker", task_id="work")
        agent.run.model_call({"n": 1}, fn=lambda _: {})
        context = agent.context()
    with SQLiteStore(path) as store:
        runtime = Runtime(store, meters=[StepMeter()], shared_budgets=[organization])
        team = Team(runtime, "mission", budget=Budget(steps=4))
        agent = team.attach(context)
        unrelated = runtime.run("unrelated")
        unrelated.model_call({"n": 1}, fn=lambda _: {})
        with pytest.raises(BudgetExceeded):
            agent.run.model_call({"n": 2}, fn=lambda _: {})
        runtime.shared_budgets = ()
        with pytest.raises(IntegrityError, match="shared budgets"):
            team.attach(context)
    with SQLiteStore(path) as store, pytest.raises(IntegrityError):
        Team(Runtime(store, meters=[StepMeter()]), "mission", budget=Budget(steps=4))


def test_concurrent_conflicting_agent_binding_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "binding.db"
    with SQLiteStore(path) as store:
        Team(Runtime(store), "mission")
    start = Barrier(2)

    def worker(role: str) -> bool:
        with SQLiteStore(path) as store:
            team = Team(Runtime(store), "mission")
            start.wait(timeout=10)
            try:
                agent = team.agent("worker", task_id="task", role=role)
            except IntegrityError:
                return False
            assert agent.identity.role == role
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ("reader", "writer")))
    assert sorted(results) == [False, True]


def test_transport_cannot_widen_capability_ceiling() -> None:
    team = Team(Runtime(registry=registry()), "mission", allowed_tools=("read",))
    context = team.agent("reader", task_id="read").context()
    with pytest.raises(IntegrityError):
        team.attach(replace(context, identity=replace(context.identity, allowed_tools=None)))
    with pytest.raises(IntegrityError):
        team.attach(
            replace(context, identity=replace(context.identity, allowed_tools=("read", "write")))
        )


def test_equivalent_decimal_limits_keep_team_identity() -> None:
    store = MemoryStore()
    first = Team(Runtime(store), "mission", budget=Budget(usd="1.0"))
    second = Team(Runtime(store), "mission", budget=Budget(usd="1.00"))
    assert first.manifest_id == second.manifest_id


def test_custom_meter_configuration_hook_excludes_operational_state() -> None:
    class StatefulMeter(StepMeter):
        def __init__(self) -> None:
            self.calls = 0

        def pollard_team_config(self) -> dict[str, str]:
            return {"version": "1"}

    meter = StatefulMeter()
    team = Team(Runtime(meters=[meter]), "mission")
    context = team.agent("worker", task_id="work").context()
    meter.calls += 1
    assert team.attach(context).identity == context.identity


def test_delegated_rollback_cannot_leave_actor_anchor() -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission", budget=Budget(steps=10))
    agent = team.agent("worker", task_id="work", budget=Budget(steps=1))
    anchor = agent.delegation_id
    agent.run.model_call({"n": 1}, fn=lambda _: {})
    attached = team.attach(agent.context())
    assert attached.run.rollback(anchor).id == anchor
    with pytest.raises(ValueError):
        attached.run.rollback(team.root_id)
    with pytest.raises(BudgetExceeded):
        attached.run.model_call({"n": 2}, fn=lambda _: {})


def test_branch_keeps_actor_anchor_and_capability_ceiling() -> None:
    team = Team(Runtime(registry=registry(), meters=[StepMeter()]), "mission")
    agent = team.agent("worker", task_id="work", allowed_tools=("read",))
    with agent.run.branch() as branch:
        assert branch.agent_identity == agent.identity
        with pytest.raises(ValueError):
            branch.rollback(team.root_id)
        branch.rollback(agent.delegation_id)
        with pytest.raises(PolicyViolation):
            branch.tool_call("write", {})


def test_public_cursor_reassignment_cannot_delegate_or_export_another_actor() -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission")
    first = team.agent("first", task_id="first")
    second = team.agent("second", task_id="second")
    first.run.cursor_id = second.run.cursor_id
    with pytest.raises(IntegrityError):
        first.context()
    with pytest.raises(IntegrityError):
        first.delegate("child", task_id="child")
    team.run.cursor_id = second.run.cursor_id
    with pytest.raises(IntegrityError):
        team.agent("top-level", task_id="top-level")


def test_public_cursor_reassignment_cannot_dispatch_as_another_actor() -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission")
    first = team.agent("first", task_id="first")
    second = team.agent("second", task_id="second")
    first.run.cursor_id = second.run.cursor_id
    dispatched: list[bool] = []
    with pytest.raises((IntegrityError, ValueError)):
        first.run.model_call({"model": "mock"}, fn=lambda _: dispatched.append(True) or {})
    assert dispatched == []


def test_parent_cannot_dispatch_from_child_actor_cursor() -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission")
    parent = team.agent("lead", task_id="plan")
    child = parent.delegate("worker", task_id="work")
    parent.run.cursor_id = child.run.cursor_id
    with pytest.raises((IntegrityError, ValueError)):
        parent.run.model_call({"model": "mock"}, fn=lambda _: {})


def test_custom_configuration_never_introspects_secrets() -> None:
    class SecretPolicy:
        def __init__(self) -> None:
            self.api_key = "not-for-the-ledger"

        def decide(self, _ctx: PolicyContext) -> Decision:
            return Decision.ALLOW

    store = MemoryStore()
    with pytest.raises(TypeError, match="pollard_team_config"):
        Team(Runtime(store, policies=[SecretPolicy()]), "mission")
    assert store.roots() == []


def test_equal_identity_nested_delegation_cannot_spoof_parent_cursor() -> None:
    team = Team(Runtime(meters=[StepMeter()]), "mission")
    parent = team.agent("same", task_id="same", budget=Budget(steps=4))
    child = parent.delegate("same", task_id="same", budget=Budget(steps=1))
    parent.run.cursor_id = child.run.cursor_id
    with pytest.raises((IntegrityError, ValueError)):
        parent.run.model_call({"model": "mock"}, fn=lambda _: {})


def test_mutating_input_budget_mapping_does_not_widen_live_team_scopes() -> None:
    team_limits = {"steps": 2}
    agent_limits = {"steps": 1}
    team = Team(Runtime(meters=[StepMeter()]), "mission", budget=Budget(extra=team_limits))
    agent = team.agent("worker", task_id="work", budget=Budget(extra=agent_limits))
    team_limits["steps"] = 100
    agent_limits["steps"] = 100
    agent.run.model_call({"n": 1}, fn=lambda _: {})
    with pytest.raises(BudgetExceeded):
        agent.run.model_call({"n": 2}, fn=lambda _: {})
    sibling = team.agent("other", task_id="work")
    sibling.run.model_call({"n": 1}, fn=lambda _: {})
    with pytest.raises(BudgetExceeded):
        sibling.run.model_call({"n": 2}, fn=lambda _: {})


@pytest.mark.parametrize("cursor", ["agent", "branch", "coordinator"])
def test_live_configuration_drift_is_rejected_before_dispatch(cursor: str) -> None:
    meter = WindowMeter("requests", 3, 60)
    runtime = Runtime(meters=[meter])
    team = Team(runtime, "mission")
    agent = team.agent("worker", task_id="work")
    run = agent.run
    if cursor == "branch":
        run = run.branch().child
    elif cursor == "coordinator":
        run = team.run
    meter.limit = meter.limit * 100
    dispatched: list[bool] = []
    with pytest.raises(IntegrityError, match="configuration"):
        run.model_call({"n": 1}, fn=lambda _: dispatched.append(True) or {})
    assert not dispatched


def test_concurrent_team_registry_binding_preserves_the_winner(tmp_path: Path) -> None:
    path = tmp_path / "registry-race.db"
    start = Barrier(2)

    class GuardedRuntime(Runtime):
        def _bind_registry(self, root_id: str) -> None:
            assert any(
                isinstance(node.payload.get("_pollard"), dict)
                and "team_manifest" in node.payload["_pollard"]  # type: ignore[operator]
                for node in self.store.walk(root_id)
            ), "team registry binding must follow the immutable manifest"
            super()._bind_registry(root_id)

    def selected_registry(name: str) -> Registry:
        return Registry(
            [ActionSpec(name, "1", name, {"type": "object"}, False, lambda _: {"ok": True})]
        )

    with SQLiteStore(path):
        pass

    def worker(name: str) -> tuple[str, str] | None:
        with SQLiteStore(path) as store:
            runtime = GuardedRuntime(store, registry=selected_registry(name))
            start.wait(timeout=10)
            try:
                team = Team(runtime, "mission")
            except IntegrityError:
                return None
            return name, team.root_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(worker, ("read", "write")) if value is not None]
    assert len(winners) == 1
    winner, root_id = winners[0]
    with SQLiteStore(path) as store:
        expected = selected_registry(winner)
        assert store.get(root_id).meta["registry_digest"] == expected.registry_digest
        reopened = Team(Runtime(store, registry=expected), "mission")
        assert reopened.root_id == root_id


@pytest.mark.parametrize("branched", [False, True])
def test_coordinator_cannot_dispatch_inside_a_delegated_actor(branched: bool) -> None:
    calls: list[bool] = []
    actions = Registry(
        [
            ActionSpec(
                "write", "1", "write", {"type": "object"}, False, lambda _: calls.append(True) or {}
            )
        ]
    )
    team = Team(Runtime(registry=actions, meters=[StepMeter()]), "mission")
    restricted = team.agent("reader", task_id="read", allowed_tools=())
    coordinator = team.run.branch().child if branched else team.run
    coordinator.cursor_id = restricted.run.cursor_id
    with pytest.raises(IntegrityError, match="delegation"):
        coordinator.tool_call("write", {})
    assert calls == []


@pytest.mark.parametrize("branched", [False, True])
def test_async_coordinator_cannot_dispatch_inside_a_delegated_actor(branched: bool) -> None:
    async def scenario() -> None:
        calls: list[bool] = []

        async def write(_args: object) -> dict[str, bool]:
            calls.append(True)
            return {"ok": True}

        actions = Registry([ActionSpec("write", "1", "write", {"type": "object"}, False, write)])
        team = Team(AsyncRuntime(registry=actions, meters=[StepMeter()]), "mission")
        restricted = team.agent("reader", task_id="read", allowed_tools=())
        coordinator = team.run.branch().child if branched else team.run
        assert isinstance(coordinator, AsyncRun)
        coordinator.cursor_id = restricted.run.cursor_id
        with pytest.raises(IntegrityError, match="delegation"):
            await coordinator.atool_call("write", {})
        assert calls == []

    asyncio.run(scenario())


def test_coordinator_branches_remain_usable_in_record_and_replay() -> None:
    store = MemoryStore()
    team = Team(Runtime(store, meters=[StepMeter()]), "mission")
    with team.run.branch(attempt=3) as branch:
        expected = branch.model_call({"input": "plan"}, fn=lambda _: {"text": "plan"})
        branch.rollback(team.manifest_id)
        with pytest.raises(ValueError):
            branch.rollback(team.root_id)
    replay = Team(Runtime(store, meters=[StepMeter()], mode="replay"), "mission")
    with replay.run.branch(attempt=3) as branch:
        actual = branch.model_call({"input": "plan"}, fn=lambda _: pytest.fail("replay dispatched"))
    assert actual.id == expected.id


@pytest.mark.parametrize("branched", [False, True])
@pytest.mark.parametrize("allowed_tools", [(), ("read",)])
def test_coordinator_obeys_team_tool_ceiling_in_record_and_replay(
    branched: bool, allowed_tools: tuple[str, ...]
) -> None:
    store = MemoryStore()
    calls: list[bool] = []
    actions = Registry(
        [
            ActionSpec(
                name,
                "1",
                name,
                {"type": "object"},
                False,
                lambda _: calls.append(True) or {"ok": True},
            )
            for name in ("read", "write")
        ]
    )
    recorded_ids: list[list[str]] = []
    for mode in ("record", "replay"):
        team = Team(
            Runtime(store, registry=actions, meters=[StepMeter()], mode=mode),
            "mission",
            allowed_tools=allowed_tools,
        )
        coordinator = team.run.branch(attempt=3).child if branched else team.run
        before = len(calls)
        with pytest.raises(PolicyViolation, match="team permissions"):
            coordinator.tool_call("write", {})
        assert len(calls) == before
        ids = [coordinator.cursor_id]
        if allowed_tools:
            read = coordinator.tool_call("read", {})
            assert read.result == {"ok": True}
            ids.append(read.id)
        else:
            with pytest.raises(PolicyViolation, match="team permissions"):
                coordinator.tool_call("read", {})
            ids.append(coordinator.cursor_id)
        recorded_ids.append(ids)
        assert calls == ([True] if allowed_tools else [])
    assert recorded_ids[0] == recorded_ids[1]


@pytest.mark.parametrize("branched", [False, True])
@pytest.mark.parametrize("allowed_tools", [(), ("read",)])
def test_async_coordinator_obeys_team_tool_ceiling_in_record_and_replay(
    branched: bool, allowed_tools: tuple[str, ...]
) -> None:
    async def scenario() -> None:
        store = MemoryStore()
        calls: list[bool] = []

        async def handler(_args: object) -> dict[str, bool]:
            calls.append(True)
            return {"ok": True}

        actions = Registry(
            [
                ActionSpec(name, "1", name, {"type": "object"}, False, handler)
                for name in ("read", "write")
            ]
        )
        recorded_ids: list[list[str]] = []
        for mode in ("record", "replay"):
            team = Team(
                AsyncRuntime(store, registry=actions, meters=[StepMeter()], mode=mode),
                "mission",
                allowed_tools=allowed_tools,
            )
            coordinator = team.run.branch(attempt=3).child if branched else team.run
            assert isinstance(coordinator, AsyncRun)
            before = len(calls)
            with pytest.raises(PolicyViolation, match="team permissions"):
                await coordinator.atool_call("write", {})
            assert len(calls) == before
            ids = [coordinator.cursor_id]
            if allowed_tools:
                read = await coordinator.atool_call("read", {})
                assert read.result == {"ok": True}
                ids.append(read.id)
            else:
                with pytest.raises(PolicyViolation, match="team permissions"):
                    await coordinator.atool_call("read", {})
                ids.append(coordinator.cursor_id)
            recorded_ids.append(ids)
            assert calls == ([True] if allowed_tools else [])
        assert recorded_ids[0] == recorded_ids[1]

    asyncio.run(scenario())
