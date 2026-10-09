"""Governed teams with separate cursors, delegated authority, and durable context."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from ._canon import IdentityValue
from .errors import IntegrityError
from .estimators.openai import OpenAITokenEstimator
from .governor import Budget
from .hashing import digest_payload
from .meters import CostMeter, DepthMeter, StepMeter, TokenMeter, WallClockMeter, WindowMeter
from .replay import ReplayMode
from .runtime import Run, Runtime, _BudgetScope, _copy_scopes
from .team_context import (
    AgentCheckpoint,
    AgentIdentity,
    BudgetContext,
    DelegationContext,
    _decimal_text,
    _validate_name,
    _validate_tools,
)
from .tree import Node, NodeKind
from .verify import verify

_SAFE_CONFIGURATION_FIELDS: dict[type[Any], tuple[str, ...]] = {
    StepMeter: ("name",),
    DepthMeter: ("name",),
    WallClockMeter: ("name",),
    TokenMeter: ("name", "_estimator", "_reserved_output_tokens", "precheck_is_estimate"),
    CostMeter: ("name", "_prices"),
    WindowMeter: ("name", "scope", "limit", "window_seconds", "_meter", "precheck_is_estimate"),
    OpenAITokenEstimator: ("_model", "_tokens_per_message"),
}


def _limits(budget: Budget | None) -> dict[str, IdentityValue] | None:
    if budget is None:
        return None
    limits = budget.limits()
    if any(not value.is_finite() for value in limits.values()):
        raise ValueError("team budget limits must be finite")
    return {name: _decimal_text(value) for name, value in sorted(limits.items())}


def _scope_contexts(scopes: list[_BudgetScope]) -> tuple[BudgetContext, ...]:
    return tuple(
        BudgetContext(
            scope.anchor_id,
            tuple((name, str(value)) for name, value in sorted(scope.budget.limits().items())),
        )
        for scope in scopes
    )


def _scopes(records: tuple[BudgetContext, ...]) -> list[_BudgetScope]:
    return [
        _BudgetScope(
            anchor_id=record.anchor_id,
            budget=Budget(extra={name: Decimal(value) for name, value in record.limits}),
        )
        for record in records
    ]


def _attenuate(
    parent: tuple[str, ...] | None, requested: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    requested = _validate_tools(requested)
    if requested is None:
        return parent
    if parent is not None and not set(requested).issubset(parent):
        raise ValueError("delegation cannot widen allowed_tools")
    return requested


def _configuration_value(value: Any, *, depth: int = 0) -> IdentityValue:
    """Describe configuration without repr, process IDs, or mutable usage counters.

    Custom meters, estimators, and policies must supply a zero-argument
    ``pollard_team_config()`` returning stable, non-sensitive JSON configuration.
    """
    if depth > 16:
        raise TypeError("cyclic or excessively nested team configuration")
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float | Decimal):
        if not math.isfinite(value):
            raise ValueError("team configuration numbers must be finite")
        return {"number": str(value)}
    if isinstance(value, tuple | list):
        return [_configuration_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("team configuration keys must be strings")
        return {
            key: _configuration_value(item, depth=depth + 1) for key, item in sorted(value.items())
        }
    kind = f"{type(value).__module__}.{type(value).__qualname__}"
    hook = getattr(value, "pollard_team_config", None)
    if callable(hook):
        return {"class": kind, "config": _configuration_value(hook(), depth=depth + 1)}
    if callable(value):
        raise TypeError("callable team configuration requires pollard_team_config()")
    fields = _SAFE_CONFIGURATION_FIELDS.get(type(value))
    if fields is None:
        raise TypeError(f"{kind} requires pollard_team_config() for team attachment")
    state = {name: getattr(value, name) for name in fields}
    return {"class": kind, "config": _configuration_value(state, depth=depth + 1)}


def _runtime_configuration(runtime: Runtime) -> dict[str, IdentityValue]:
    return {
        "registry_digest": None if runtime.registry is None else runtime.registry.registry_digest,
        "meters": [_configuration_value(meter) for meter in runtime.meters],
        "policies": [_configuration_value(policy) for policy in runtime.policies],
        "dry_run": runtime.dry_run,
    }


def _event(node: Node, name: str) -> dict[str, IdentityValue]:
    wrapper = node.payload.get("_pollard")
    if node.kind != NodeKind.NOTE.value or not isinstance(wrapper, dict):
        raise IntegrityError(f"expected a recorded {name} note")
    event = wrapper.get(name)
    if not isinstance(event, dict) or event.get("version") != 1:
        raise IntegrityError(f"invalid recorded {name} note")
    return event


class Team:
    """Bind worker cursors to one immutable mission configuration.

    A Team governs calls; the caller still schedules workers and transports
    messages. Give each concurrently active worker its own TeamAgent cursor.
    """

    def __init__(
        self,
        runtime: Runtime,
        label: str,
        *,
        budget: Budget | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        attempt: int = 0,
    ) -> None:
        from .aio import AsyncRun, AsyncRuntime

        _validate_name(label, "team label")
        allowed_tools = _validate_tools(allowed_tools)
        self._runtime = runtime
        self.label = label
        self._allowed_tools = allowed_tools
        self._require_registry(allowed_tools)
        budget_limits = _limits(budget)
        configuration = _runtime_configuration(runtime)
        root = runtime._put(
            Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        )
        existing_registry = root.meta.get("registry_digest")
        if existing_registry is not None and existing_registry != configuration["registry_digest"]:
            raise IntegrityError("team root is already bound to a different registry")
        run_class = AsyncRun if isinstance(runtime, AsyncRuntime) else Run
        self.run = run_class(
            runtime=runtime,
            root_id=root.id,
            cursor_id=root.id,
            label=label,
            budget_scopes=runtime._run_scopes(root.id, budget),
        )
        self.root_id = root.id
        self._initial_scopes = _scope_contexts(self.run._budget_scopes)
        self.run._budget_scopes = _scopes(self._initial_scopes)
        manifest: dict[str, IdentityValue] = {
            "version": 1,
            "team_id": self.root_id,
            "label": label,
            "budget": budget_limits,
            "allowed_tools": None if allowed_tools is None else list(allowed_tools),
            "configuration": configuration,
            "scopes": [scope.to_dict() for scope in self._initial_scopes],
        }
        node = self._bind_note(
            self.root_id,
            {"team_manifest_slot": {"version": 1}},
            "team_manifest",
            manifest,
        )
        # Only the winning immutable manifest may bind mutable legacy root
        # metadata. A conflicting first-time creator must leave it untouched.
        runtime._bind_registry(self.root_id)
        self.manifest_id = node.id
        self._configuration = configuration
        self._manifest_payload = node.payload
        self._config_digest = digest_payload(configuration)
        self.run.cursor_id = node.id
        self.run._agent_anchor_id = node.id
        self.run._team_validator = self._check_configuration

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        return self._allowed_tools

    def _require_registry(self, tools: tuple[str, ...] | None) -> None:
        if tools is not None and self._runtime.registry is None:
            raise ValueError("restricted team actors require a registry")
        if tools is not None and self._runtime.registry is not None:
            for name in tools:
                if name not in self._runtime.registry:
                    raise ValueError(f"allowed tool is absent from registry: {name}")

    def _check_configuration(self) -> None:
        if _runtime_configuration(self._runtime) != self._configuration:
            raise IntegrityError("runtime configuration differs from the team manifest")
        # Named organization scopes are reconstructed by Runtime.run, then
        # checked here together with the common mission budget on attachment.
        if _scope_contexts(self.run._budget_scopes) != self._initial_scopes:
            raise IntegrityError("team budget scope configuration has changed")
        declared = tuple(
            BudgetContext(
                scope.scope_id,
                tuple((name, str(value)) for name, value in sorted(scope.budget.limits().items())),
            )
            for scope in self._runtime.shared_budgets
        )
        expected = tuple(scope for scope in self._initial_scopes if scope.anchor_id != self.root_id)
        if declared != expected:
            raise IntegrityError("runtime shared budgets differ from the team manifest")
        self._runtime._validate_shared_configuration()
        manifest = self._runtime.store.get(self.manifest_id)
        if (
            manifest.payload != self._manifest_payload
            or not verify(self._runtime.store, self.manifest_id).ok
        ):
            raise IntegrityError("team manifest failed integrity validation")
        self._validate_binding(manifest)

    def _bind_note(
        self,
        parent_id: str,
        slot_payload: dict[str, IdentityValue],
        event_name: str,
        event_payload: dict[str, IdentityValue],
    ) -> Node:
        """Use first-writer store insertion to bind a stable name to immutable data."""
        slot = Node.make(kind=NodeKind.NOTE, parent=parent_id, payload={"_pollard": slot_payload})
        note = Node.make(
            kind=NodeKind.NOTE,
            parent=slot.id,
            payload={"_pollard": {event_name: event_payload}},
        )
        binding = Node.make(
            kind=NodeKind.NOTE,
            parent=parent_id,
            payload=slot.payload,
            result={"bound_node_id": note.id},
        )
        stored = self._runtime._put(binding)
        if not verify(self._runtime.store, stored.id).ok or stored.result != binding.result:
            raise IntegrityError(f"conflicting reuse of {event_name} identity")
        recorded = self._runtime._put(note)
        if not verify(self._runtime.store, recorded.id).ok:
            raise IntegrityError(f"recorded {event_name} failed integrity verification")
        return recorded

    def agent(
        self,
        agent_id: str,
        *,
        task_id: str,
        role: str | None = None,
        budget: Budget | None = None,
        allowed_tools: tuple[str, ...] | None = None,
    ) -> TeamAgent:
        return self._delegate(
            self.run,
            agent_id,
            task_id=task_id,
            role=role,
            budget=budget,
            allowed_tools=allowed_tools,
            parent_identity=None,
        )

    def _delegate(
        self,
        parent: Run,
        agent_id: str,
        *,
        task_id: str,
        role: str | None,
        budget: Budget | None,
        allowed_tools: tuple[str, ...] | None,
        parent_identity: AgentIdentity | None,
    ) -> TeamAgent:
        self._check_configuration()
        if parent_identity is None:
            self._validate_coordinator_cursor(parent.cursor_id)
        ceiling = self.allowed_tools if parent_identity is None else parent_identity.allowed_tools
        tools = _attenuate(ceiling, allowed_tools)
        self._require_registry(tools)
        identity = AgentIdentity(self.root_id, agent_id, task_id, role, tools)
        local_limits = _limits(budget)
        inherited = _scope_contexts(parent._budget_scopes)
        payload: dict[str, IdentityValue] = {
            "version": 1,
            "identity": identity.to_dict(),
            "parent_agent_id": None if parent_identity is None else parent_identity.agent_id,
            "budget": local_limits,
            "inherited_scopes": [scope.to_dict() for scope in inherited],
            "manifest_id": self.manifest_id,
        }
        if self._runtime.mode != ReplayMode.REPLAY:
            parent._precheck(NodeKind.NOTE.value, {"_pollard": {"team_agent": payload}})
        anchor = self._bind_note(
            parent.cursor_id,
            {"team_agent_slot": {"version": 1, "agent_id": agent_id, "task_id": task_id}},
            "team_agent",
            payload,
        )
        scopes = _copy_scopes(parent._budget_scopes)
        if budget is not None:
            local_scope = BudgetContext.from_dict({"anchor_id": anchor.id, "limits": local_limits})
            scopes.extend(_scopes((local_scope,)))
        child = type(parent)(
            runtime=self._runtime,
            root_id=self.root_id,
            cursor_id=anchor.id,
            label=self.label,
            budget_scopes=scopes,
            agent_identity=identity,
            agent_anchor_id=anchor.id,
            team_validator=self._check_configuration,
        )
        return TeamAgent(self, child, anchor.id, identity)

    def _validate_coordinator_cursor(self, cursor_id: str) -> None:
        if not verify(self._runtime.store, cursor_id).ok:
            raise IntegrityError("coordinator cursor failed ancestry verification")
        cursor: str | None = cursor_id
        while cursor is not None:
            if cursor == self.manifest_id:
                return
            node = self._runtime.store.get(cursor)
            wrapper = node.payload.get("_pollard")
            if isinstance(wrapper, dict) and "team_agent" in wrapper:
                raise IntegrityError("coordinator cursor cannot impersonate a delegated actor")
            cursor = node.parent
        raise IntegrityError("coordinator cursor is outside the team manifest")

    def attach(self, context: DelegationContext | dict[str, Any]) -> TeamAgent:
        """Validate transported authority and recreate precisely that worker cursor."""
        if isinstance(context, dict):
            context = DelegationContext.from_dict(context)
        if not isinstance(context, DelegationContext):
            raise TypeError("attach requires a DelegationContext or its dictionary")
        self._check_configuration()
        if (
            context.root_id != self.root_id
            or context.manifest_id != self.manifest_id
            or context.config_digest != self._config_digest
        ):
            raise IntegrityError("delegation context belongs to another team configuration")
        report = verify(self._runtime.store, context.cursor_id)
        if not report.ok:
            raise IntegrityError("delegation context ancestry failed verification")
        chain: list[Node] = []
        cursor: str | None = context.cursor_id
        while cursor is not None:
            node = self._runtime.store.get(cursor)
            chain.append(node)
            cursor = node.parent
        ids = {node.id for node in chain}
        if (
            self.root_id not in ids
            or self.manifest_id not in ids
            or context.delegation_id not in ids
        ):
            raise IntegrityError("cursor is outside the delegated ancestry")
        expected_scopes = self._initial_scopes
        expected_identity: AgentIdentity | None = None
        latest_delegation: str | None = None
        for node in reversed(chain):
            wrapper = node.payload.get("_pollard")
            if not isinstance(wrapper, dict) or "team_agent" not in wrapper:
                continue
            event = _event(node, "team_agent")
            if event.get("manifest_id") != self.manifest_id:
                raise IntegrityError("delegation references another team manifest")
            identity = AgentIdentity.from_dict(event.get("identity"))
            if identity.team_id != self.root_id:
                raise IntegrityError("delegation actor belongs to another team")
            parent_id = None if expected_identity is None else expected_identity.agent_id
            if event.get("parent_agent_id") != parent_id:
                raise IntegrityError("delegation parent identity is inconsistent")
            ceiling = (
                self.allowed_tools if expected_identity is None else expected_identity.allowed_tools
            )
            if _attenuate(ceiling, identity.allowed_tools) != identity.allowed_tools:
                raise IntegrityError("delegation widens inherited tool authority")
            inherited = event.get("inherited_scopes")
            if inherited != [scope.to_dict() for scope in expected_scopes]:
                raise IntegrityError("delegation changed inherited budget scopes")
            local = event.get("budget")
            if local is not None:
                expected_scopes = (
                    *expected_scopes,
                    BudgetContext.from_dict({"anchor_id": node.id, "limits": local}),
                )
            self._validate_binding(node)
            expected_identity = identity
            latest_delegation = node.id
        if (
            latest_delegation != context.delegation_id
            or expected_identity != context.identity
            or expected_scopes != context.scopes
        ):
            raise IntegrityError(
                "transported identity or budget authority differs from the recording"
            )
        self._require_registry(context.identity.allowed_tools)
        child = type(self.run)(
            runtime=self._runtime,
            root_id=self.root_id,
            cursor_id=context.cursor_id,
            label=self.label,
            budget_scopes=_scopes(context.scopes),
            agent_identity=context.identity,
            agent_anchor_id=context.delegation_id,
            team_validator=self._check_configuration,
        )
        return TeamAgent(self, child, context.delegation_id, context.identity)

    def _validate_binding(self, node: Node) -> None:
        if node.parent is None:
            raise IntegrityError("delegation binding is missing")
        binding = self._runtime.store.get(node.parent)
        if binding.result != {"bound_node_id": node.id}:
            raise IntegrityError("delegation binding does not match recorded authority")

    def restore(self, checkpoint: AgentCheckpoint | dict[str, Any]) -> TeamAgent:
        """Restore a recorded cursor without choosing another worker's deepest leaf."""
        if isinstance(checkpoint, dict):
            checkpoint = AgentCheckpoint.from_dict(checkpoint)
        if not isinstance(checkpoint, AgentCheckpoint):
            raise TypeError("restore requires an AgentCheckpoint or its dictionary")
        report = verify(self._runtime.store, checkpoint.checkpoint_id)
        if not report.ok:
            raise IntegrityError("checkpoint ancestry failed verification")
        node = self._runtime.store.get(checkpoint.checkpoint_id)
        payload = _event(node, "team_checkpoint")
        if (
            payload.get("context") != checkpoint.context.to_dict()
            or node.parent != checkpoint.context.cursor_id
        ):
            raise IntegrityError("checkpoint context differs from its immutable recording")
        return self.attach(checkpoint.context)


class TeamAgent:
    """One worker's isolated cursor and inherited delegation authority."""

    def __init__(self, team: Team, run: Run, delegation_id: str, identity: AgentIdentity) -> None:
        self.team = team
        self.run = run
        self.delegation_id = delegation_id
        self._identity = identity

    @property
    def identity(self) -> AgentIdentity:
        return self._identity

    def context(self) -> DelegationContext:
        self.team._check_configuration()
        context = DelegationContext(
            root_id=self.team.root_id,
            manifest_id=self.team.manifest_id,
            delegation_id=self.delegation_id,
            cursor_id=self.run.cursor_id,
            identity=self.identity,
            scopes=_scope_contexts(self.run._budget_scopes),
            config_digest=self.team._config_digest,
        )
        self.team.attach(context)
        return context

    def delegate(
        self,
        agent_id: str,
        *,
        task_id: str,
        role: str | None = None,
        budget: Budget | None = None,
        allowed_tools: tuple[str, ...] | None = None,
    ) -> TeamAgent:
        self.context()
        return self.team._delegate(
            self.run,
            agent_id,
            task_id=task_id,
            role=role,
            budget=budget,
            allowed_tools=allowed_tools,
            parent_identity=self.identity,
        )

    def checkpoint(self) -> AgentCheckpoint:
        context = self.context()
        payload: dict[str, IdentityValue] = {
            "_pollard": {"team_checkpoint": {"version": 1, "context": context.to_dict()}}
        }
        # The checkpoint is a side note. It does not move the executable cursor,
        # so restoring it resumes the exact call identity and budget ancestry.
        node = Node.make(kind=NodeKind.NOTE, parent=context.cursor_id, payload=payload)
        stored = self.team._runtime._put(node)
        return AgentCheckpoint(stored.id, context)


__all__ = ["Team", "TeamAgent"]
