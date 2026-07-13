"""Sync runtime for governed execution trees."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from inspect import isawaitable
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, Protocol

from ._canon import IdentityValue
from .errors import BudgetExceeded, ConfirmationRequired, IntegrityError, PolicyViolation
from .governor import (
    Budget,
    BudgetCheck,
    charge_to_decimal,
    charge_to_json,
    check_budget,
    exhausted_after_settle,
    recompute_charges,
    spent_decimal,
)
from .hashing import digest_payload
from .meters import DepthMeter, Meter, StepMeter, TokenMeter, WallClockMeter
from .policy import Decision, Policy, PolicyContext
from .registry import ActionSpec, Registry
from .replay import ReplayMode, avoided_charges, normalize_mode, recorded_node_or_missing
from .store import MemoryStore, Store
from .stores import SQLiteStore
from .tree import Node, NodeKind

StepFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class _BudgetScope:
    budget: Budget
    anchor_id: str
    exhausted: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _PendingToolCall:
    parent_id: str
    payload: dict[str, IdentityValue]
    args: dict[str, IdentityValue]
    spec: ActionSpec
    attempt: int


class Runtime:
    def __init__(
        self,
        store: str | Path | Store | None = None,
        *,
        meters: list[Meter] | None = None,
        registry: Registry | None = None,
        policies: list[Policy] | None = None,
        dry_run: bool = False,
        mode: str | ReplayMode = ReplayMode.RECORD,
    ) -> None:
        self.store: Store = _coerce_store(store)
        self.meters = meters or [StepMeter(), DepthMeter(), WallClockMeter(), TokenMeter()]
        self.registry = registry
        self.policies = policies or []
        self.dry_run = dry_run
        self.mode = normalize_mode(mode)

    def run(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> Run:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        if not self.store.exists(root.id):
            self.store.put(root)
        else:
            root = self.store.get(root.id)
        self._bind_registry(root.id)
        scopes = [] if budget is None else [_BudgetScope(budget=budget, anchor_id=root.id)]
        return Run(
            runtime=self,
            root_id=root.id,
            cursor_id=root.id,
            label=label,
            budget_scopes=scopes,
        )

    def resume(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> Run:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        stored_root = self.store.get(root.id)
        self._bind_registry(stored_root.id)
        scopes = [] if budget is None else [_BudgetScope(budget=budget, anchor_id=stored_root.id)]
        return Run(
            runtime=self,
            root_id=stored_root.id,
            cursor_id=_deepest_non_pruned_leaf(self.store, stored_root.id),
            label=label,
            budget_scopes=scopes,
        )

    def _bind_registry(self, root_id: str) -> None:
        if self.registry is None:
            return
        root = self.store.get(root_id)
        existing = root.meta.get("registry_digest")
        if existing is not None and existing != self.registry.registry_digest:
            raise IntegrityError("run root is already bound to a different registry")
        if existing is None:
            self.store.update_meta(root_id, {"registry_digest": self.registry.registry_digest})


class Run:
    def __init__(
        self,
        *,
        runtime: Runtime,
        root_id: str,
        cursor_id: str,
        label: str,
        budget_scopes: list[_BudgetScope],
    ) -> None:
        self._runtime = runtime
        self.root_id = root_id
        self.cursor_id = cursor_id
        self.label = label
        self._budget_scopes = budget_scopes
        self._avoided: dict[str, float] = {}
        self._pending_tool_calls: dict[str, _PendingToolCall] = {}

    @property
    def store(self) -> Store:
        return self._runtime.store

    @property
    def cursor(self) -> Node:
        return self.store.get(self.cursor_id)

    def __enter__(self) -> Run:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    def model_call(
        self,
        payload: dict[str, IdentityValue],
        *,
        fn: StepFn,
        attempt: int = 0,
    ) -> Node:
        return self._call(NodeKind.MODEL_CALL, payload, fn=fn, attempt=attempt)

    def tool_call(
        self,
        name: str,
        args: dict[str, IdentityValue],
        *,
        fn: StepFn | None = None,
        version: str | None = None,
        attempt: int = 0,
    ) -> Node:
        if self._runtime.registry is not None:
            return self._registered_tool_call(name, args, version=version, attempt=attempt)
        if fn is None:
            raise TypeError("unfenced tool_call requires fn")
        payload: dict[str, IdentityValue] = {"tool": name, "args": args}
        return self._call(NodeKind.TOOL_CALL, payload, fn=fn, attempt=attempt)

    def confirm(self, token: str) -> Node:
        pending = self._pending_tool_calls.pop(token)
        if self.cursor_id != pending.parent_id:
            raise ValueError("cannot confirm after cursor moved")
        if pending.spec.handler is None:
            self._refuse_policy("registered action has no handler", pending.payload)
        return self._call(
            NodeKind.TOOL_CALL,
            pending.payload,
            fn=_registered_handler(pending.spec, pending.args),
            attempt=pending.attempt,
        )

    def note(self, payload: dict[str, IdentityValue], *, attempt: int = 0) -> Node:
        self._precheck(NodeKind.NOTE.value, payload)
        node = Node.make(
            kind=NodeKind.NOTE,
            parent=self.cursor_id,
            payload=payload,
            attempt=attempt,
            meta={"created_at": _now_utc()},
        )
        self.store.put(node)
        self.cursor_id = node.id
        return node

    def branch(self, *, attempt: int = 0, budget: Budget | None = None) -> RunBranch:
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
        child = Run(
            runtime=self._runtime,
            root_id=self.root_id,
            cursor_id=anchor.id,
            label=self.label,
            budget_scopes=scopes,
        )
        return RunBranch(parent=self, child=child)

    def rollback(self, node_id: str | None = None, *, steps: int = 1) -> Node:
        target = node_id
        if target is None:
            target = self.cursor_id
            for _ in range(steps):
                parent = self.store.get(target).parent
                if parent is None:
                    break
                target = parent
        self._ensure_ancestor(target)
        self.cursor_id = target
        return self.cursor

    def prune(self) -> None:
        self.store.update_meta(self.cursor_id, {"pruned": True})

    def report(self) -> dict[str, dict[str, float]]:
        return {
            "spent": recompute_charges(self.store, self.root_id),
            "avoided": dict(self._avoided),
        }

    def _call(
        self,
        kind: NodeKind,
        payload: dict[str, IdentityValue],
        *,
        fn: StepFn,
        attempt: int,
    ) -> Node:
        recorded = self._recorded_node(kind, payload, attempt)
        if recorded is not None:
            return recorded
        self._precheck(kind.value, payload)
        measurements = _start_measurements(self._runtime.meters)
        start = time.perf_counter()
        try:
            result = fn(payload)
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

    def _registered_tool_call(
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
        recorded = self._recorded_node(NodeKind.TOOL_CALL, payload, attempt)
        if recorded is not None:
            return recorded
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
        return self._call(
            NodeKind.TOOL_CALL,
            payload,
            fn=_registered_handler(spec, args),
            attempt=attempt,
        )

    def _charges(
        self,
        kind: str,
        payload: dict[str, IdentityValue],
        result: dict[str, Any],
        meta: dict[str, Any],
    ) -> dict[str, int | float]:
        charges: dict[str, int | float] = {}
        payload_any: dict[str, Any] = payload
        for meter in self._runtime.meters:
            amount = charge_to_decimal(meter.charge(kind, payload_any, result, meta))
            if amount != 0:
                charges[meter.name] = charge_to_json(amount)
        return charges

    def _recorded_node(
        self,
        kind: NodeKind,
        payload: dict[str, IdentityValue],
        attempt: int,
    ) -> Node | None:
        node = recorded_node_or_missing(
            mode=self._runtime.mode,
            store=self.store,
            kind=kind,
            parent_id=self.cursor_id,
            payload=payload,
            attempt=attempt,
        )
        if node is None:
            return None
        charges = avoided_charges(
            meters=self._runtime.meters,
            kind=kind.value,
            payload=payload,
            node=node,
        )
        self._add_avoided(charges)
        self.cursor_id = node.id
        return node

    def _add_avoided(self, charges: dict[str, int | float]) -> None:
        for name, amount in charges.items():
            total = charge_to_decimal(self._avoided.get(name, 0)) + charge_to_decimal(amount)
            self._avoided[name] = float(total)

    def _precheck(self, kind: str, payload: dict[str, IdentityValue]) -> None:
        estimates = self._estimates(kind, payload)
        for scope in self._budget_scopes:
            check = check_budget(
                budget=scope.budget,
                spent=spent_decimal(self.store, scope.anchor_id),
                estimates=estimates,
                exhausted=scope.exhausted,
            )
            if not check.ok:
                self._refuse(check, kind, payload)

    def _estimates(self, kind: str, payload: dict[str, IdentityValue]) -> dict[str, Decimal]:
        estimates: dict[str, Decimal] = {}
        payload_any: dict[str, Any] = payload
        for meter in self._runtime.meters:
            estimate = meter.precheck_estimate(kind, payload_any)
            if estimate is not None:
                estimates[meter.name] = charge_to_decimal(estimate)
        next_depth = _depth(self.store, self.cursor_id) + 1
        estimates["depth"] = Decimal(next_depth)
        return estimates

    def _refuse(
        self,
        check: BudgetCheck,
        blocked_kind: str,
        blocked_payload: dict[str, IdentityValue],
    ) -> None:
        meter = check.meter or "unknown"
        payload: dict[str, IdentityValue] = {
            "reason": "budget",
            "meter": meter,
            "requested": str(check.requested),
            "remaining": str(check.remaining),
            "blocked_kind": blocked_kind,
            "blocked_payload_digest": digest_payload(blocked_payload),
        }
        node = Node.make(
            kind=NodeKind.REFUSAL,
            parent=self.cursor_id,
            payload=payload,
            meta={"created_at": _now_utc()},
        )
        self.store.put(node)
        self.cursor_id = node.id
        raise BudgetExceeded(f"budget exceeded for {meter}", node.id)

    def _refuse_policy(
        self,
        detail: str,
        blocked_payload: dict[str, IdentityValue],
    ) -> NoReturn:
        payload: dict[str, IdentityValue] = {
            "reason": "policy",
            "detail": detail,
            "blocked_kind": "tool_call",
            "blocked_payload_digest": digest_payload(blocked_payload),
        }
        registry = self._runtime.registry
        if registry is not None:
            payload["registry_digest"] = registry.registry_digest
        node = Node.make(
            kind=NodeKind.REFUSAL,
            parent=self.cursor_id,
            payload=payload,
            meta={"created_at": _now_utc()},
        )
        self.store.put(node)
        self.cursor_id = node.id
        raise PolicyViolation(detail, node.id)

    def _settle_scopes(self) -> None:
        for scope in self._budget_scopes:
            scope.exhausted.update(
                exhausted_after_settle(
                    budget=scope.budget,
                    spent=spent_decimal(self.store, scope.anchor_id),
                )
            )

    def _ensure_ancestor(self, node_id: str) -> None:
        current: str | None = self.cursor_id
        while current is not None:
            if current == node_id:
                return
            current = self.store.get(current).parent
        raise ValueError(f"{node_id} is not an ancestor of the cursor")


@dataclass
class RunBranch:
    parent: Run
    child: Run

    def __enter__(self) -> Run:
        return self.child

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


class _Measurement(Protocol):
    def __enter__(self) -> _Measurement: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def readings(self) -> dict[str, float]: ...


def _coerce_store(store: str | Path | Store | None) -> Store:
    if store is None:
        return MemoryStore()
    if isinstance(store, str | Path):
        return SQLiteStore(store)
    return store


def _copy_scopes(scopes: list[_BudgetScope]) -> list[_BudgetScope]:
    return [
        _BudgetScope(
            budget=scope.budget,
            anchor_id=scope.anchor_id,
            exhausted=set(scope.exhausted),
        )
        for scope in scopes
    ]


def _start_measurements(meters: list[Meter]) -> list[_Measurement]:
    measurements: list[_Measurement] = []
    for meter in meters:
        measure = getattr(meter, "measure", None)
        if not callable(measure):
            continue
        measurement = measure()
        measurement.__enter__()
        measurements.append(measurement)
    return measurements


def _stop_measurements(measurements: list[_Measurement]) -> None:
    for measurement in reversed(measurements):
        measurement.__exit__(None, None, None)


def _registered_handler(
    spec: ActionSpec,
    args: dict[str, IdentityValue],
) -> StepFn:
    if spec.handler is None:
        raise RuntimeError("registered handler is missing")
    handler = spec.handler

    def call(_payload: dict[str, Any]) -> dict[str, Any]:
        result = handler(args)
        if isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise TypeError("async registered handler requires AsyncRuntime")
        return result

    return call


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _depth(store: Store, node_id: str) -> int:
    depth = 0
    current = store.get(node_id).parent
    while current is not None:
        depth += 1
        current = store.get(current).parent
    return depth


def _deepest_non_pruned_leaf(store: Store, root_id: str) -> str:
    best_id = root_id
    best_depth = 0
    for node in store.walk(root_id):
        if node.meta.get("pruned") is True:
            continue
        children = [
            child_id
            for child_id in store.children(node.id)
            if store.get(child_id).meta.get("pruned") is not True
        ]
        if children:
            continue
        node_depth = _depth(store, node.id)
        if node_depth > best_depth or (node_depth == best_depth and node.id < best_id):
            best_depth = node_depth
            best_id = node.id
    return best_id
