"""Async runtime mirror."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from inspect import isawaitable
from pathlib import Path
from typing import Any

from ._canon import IdentityValue
from .errors import ConfirmationRequired
from .governor import Budget
from .meters import Meter
from .policy import Decision, Policy, PolicyContext
from .registry import ActionSpec, Registry
from .runtime import (
    Run,
    RunBranch,
    Runtime,
    _BudgetScope,
    _copy_scopes,
    _deepest_non_pruned_leaf,
    _PendingToolCall,
    _start_measurements,
    _stop_measurements,
)
from .store import Store
from .tree import Node, NodeKind

AsyncStepFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AsyncRuntime(Runtime):
    def __init__(
        self,
        store: str | Path | Store | None = None,
        *,
        meters: list[Meter] | None = None,
        registry: Registry | None = None,
        policies: list[Policy] | None = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            store,
            meters=meters,
            registry=registry,
            policies=policies,
            dry_run=dry_run,
        )

    def run(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> AsyncRun:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        if not self.store.exists(root.id):
            self.store.put(root)
        else:
            root = self.store.get(root.id)
        self._bind_registry(root.id)
        scopes = [] if budget is None else [_BudgetScope(budget=budget, anchor_id=root.id)]
        return AsyncRun(
            runtime=self,
            root_id=root.id,
            cursor_id=root.id,
            label=label,
            budget_scopes=scopes,
        )

    def resume(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> AsyncRun:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        stored_root = self.store.get(root.id)
        self._bind_registry(stored_root.id)
        scopes = [] if budget is None else [_BudgetScope(budget=budget, anchor_id=stored_root.id)]
        return AsyncRun(
            runtime=self,
            root_id=stored_root.id,
            cursor_id=_deepest_non_pruned_leaf(self.store, stored_root.id),
            label=label,
            budget_scopes=scopes,
        )


class AsyncRun(Run):
    def __enter__(self) -> AsyncRun:
        return self

    async def __aenter__(self) -> AsyncRun:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    async def amodel_call(
        self,
        payload: dict[str, IdentityValue],
        *,
        fn: AsyncStepFn,
        attempt: int = 0,
    ) -> Node:
        return await self._acall(NodeKind.MODEL_CALL, payload, fn=fn, attempt=attempt)

    async def atool_call(
        self,
        name: str,
        args: dict[str, IdentityValue],
        *,
        fn: AsyncStepFn | None = None,
        version: str | None = None,
        attempt: int = 0,
    ) -> Node:
        if self._runtime.registry is not None:
            return await self._aregistered_tool_call(name, args, version=version, attempt=attempt)
        if fn is None:
            raise TypeError("unfenced atool_call requires fn")
        payload: dict[str, IdentityValue] = {"tool": name, "args": args}
        return await self._acall(NodeKind.TOOL_CALL, payload, fn=fn, attempt=attempt)

    async def aconfirm(self, token: str) -> Node:
        pending = self._pending_tool_calls.pop(token)
        if self.cursor_id != pending.parent_id:
            raise ValueError("cannot confirm after cursor moved")
        if pending.spec.handler is None:
            self._refuse_policy("registered action has no handler", pending.payload)
        return await self._acall(
            NodeKind.TOOL_CALL,
            pending.payload,
            fn=_async_registered_handler(pending.spec, pending.args),
            attempt=pending.attempt,
        )

    def branch(self, *, attempt: int = 0, budget: Budget | None = None) -> AsyncRunBranch:
        payload: dict[str, IdentityValue] = {"branch": True}
        self._precheck(NodeKind.NOTE.value, payload)
        anchor = Node.make(
            kind=NodeKind.NOTE,
            parent=self.cursor_id,
            payload=payload,
            attempt=attempt,
            meta={"created_at": _now_utc()},
        )
        self.store.put(anchor)
        scopes = [*_copy_scopes(self._budget_scopes)]
        if budget is not None:
            scopes.append(_BudgetScope(budget=budget, anchor_id=anchor.id))
        child = AsyncRun(
            runtime=self._runtime,
            root_id=self.root_id,
            cursor_id=anchor.id,
            label=self.label,
            budget_scopes=scopes,
        )
        return AsyncRunBranch(parent=self, child=child)

    async def _acall(
        self,
        kind: NodeKind,
        payload: dict[str, IdentityValue],
        *,
        fn: AsyncStepFn,
        attempt: int,
    ) -> Node:
        self._precheck(kind.value, payload)
        measurements = _start_measurements(self._runtime.meters)
        start = time.perf_counter()
        try:
            result = await fn(payload)
        finally:
            duration = time.perf_counter() - start
            _stop_measurements(measurements)
        meta: dict[str, Any] = {"created_at": _now_utc(), "duration_s": duration}
        for measurement in measurements:
            meta.update(measurement.readings())
        charges = self._charges(kind.value, payload, result, meta)
        meta["charges"] = charges
        if isinstance(result, dict) and isinstance(result.get("usage"), dict):
            meta["usage"] = result["usage"]
        node = Node.make(
            kind=kind,
            parent=self.cursor_id,
            payload=payload,
            attempt=attempt,
            result=result,
            meta=meta,
        )
        self.store.put(node)
        self.cursor_id = node.id
        self._settle_scopes()
        return self.store.get(node.id)

    async def _aregistered_tool_call(
        self,
        name: str,
        args: dict[str, IdentityValue],
        *,
        version: str | None,
        attempt: int,
    ) -> Node:
        registry = self._runtime.registry
        if registry is None:
            raise RuntimeError("registered tool call requires a registry")
        blocked_payload: dict[str, IdentityValue] = {"tool": name, "args": args}
        try:
            spec = registry.get(name, version)
        except KeyError:
            requested = name if version is None else f"{name}@{version}"
            self._refuse_policy(
                f"unknown registered action: {requested}",
                blocked_payload,
            )
        finding = spec.validate_args(args)
        if finding is not None:
            self._refuse_policy(f"schema validation failed: {finding}", blocked_payload)
        payload: dict[str, IdentityValue] = {
            "tool": spec.name,
            "version": spec.version,
            "args": args,
            "spec_digest": spec.spec_digest,
            "registry_digest": registry.registry_digest,
        }
        for policy in self._runtime.policies:
            decision = policy.decide(
                PolicyContext(
                    spec=spec,
                    args=args,
                    cursor_id=self.cursor_id,
                    run_label=self.label,
                    counters=self.report()["spent"],
                )
            )
            if decision == Decision.ALLOW:
                continue
            if decision == Decision.DENY:
                self._refuse_policy("denied by policy", payload)
            if decision == Decision.CONFIRM:
                prepared = Node.make(
                    kind=NodeKind.TOOL_CALL,
                    parent=self.cursor_id,
                    payload=payload,
                    attempt=attempt,
                )
                self._pending_tool_calls[prepared.id] = _PendingToolCall(
                    parent_id=self.cursor_id,
                    payload=payload,
                    args=args,
                    spec=spec,
                    attempt=attempt,
                )
                raise ConfirmationRequired("confirmation required by policy", prepared.id)
        if self._runtime.dry_run and spec.side_effects:
            self._precheck(NodeKind.TOOL_CALL.value, payload)
            node = Node.make(
                kind=NodeKind.TOOL_CALL,
                parent=self.cursor_id,
                payload=payload,
                attempt=attempt,
                meta={
                    "created_at": _now_utc(),
                    "dry_run": True,
                    "charges": {"steps": 1},
                },
            )
            self.store.put(node)
            self.cursor_id = node.id
            self._settle_scopes()
            return self.store.get(node.id)
        if spec.handler is None:
            self._refuse_policy("registered action has no handler", payload)
        return await self._acall(
            NodeKind.TOOL_CALL,
            payload,
            fn=_async_registered_handler(spec, args),
            attempt=attempt,
        )


class AsyncRunBranch(RunBranch):
    child: AsyncRun

    def __enter__(self) -> AsyncRun:
        return self.child

    async def __aenter__(self) -> AsyncRun:
        return self.child

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


def _async_registered_handler(
    spec: ActionSpec,
    args: dict[str, IdentityValue],
) -> AsyncStepFn:
    if spec.handler is None:
        raise RuntimeError("registered handler is missing")
    handler = spec.handler

    async def call(_payload: dict[str, Any]) -> dict[str, Any]:
        result = handler(args)
        if isawaitable(result):
            return await result
        return result

    return call


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
