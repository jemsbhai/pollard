"""Sync runtime for governed execution trees."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from inspect import isawaitable
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Any, NoReturn, Protocol
from uuid import uuid4

from ._canon import IdentityValue
from .arbiter import (
    BudgetReservation,
    RenewableArbiter,
    TransactionalArbiter,
    WindowReservation,
)
from .errors import (
    BudgetExceeded,
    ConfirmationRequired,
    IntegrityError,
    PolicyViolation,
    ReservationLeaseLost,
)
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
from .meters import (
    DepthMeter,
    Meter,
    StepMeter,
    TokenMeter,
    WallClockMeter,
    WindowMeter,
)
from .policy import Decision, Policy, PolicyContext
from .registry import ActionSpec, Registry
from .replay import (
    ReplayMode,
    avoided_charges,
    normalize_mode,
    record_avoided_charges,
    recorded_node_or_missing,
)
from .store import MemoryStore, Store
from .stores import SQLiteStore
from .tree import Node, NodeKind

DeltaCallback = Callable[[dict[str, Any]], None]
NodeCallback = Callable[[Node], None]
StepResult = dict[str, Any] | Iterator[dict[str, Any]]
StepFn = Callable[[dict[str, Any]], StepResult]


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


class _LeaseHeartbeat:
    def __init__(
        self,
        *,
        reservation_id: str,
        lease_seconds: float,
        renew: Callable[[str, float], bool],
    ) -> None:
        self._reservation_id = reservation_id
        self._lease_seconds = lease_seconds
        self._renew = renew
        self._stop = Event()
        self._lost: str | None = None
        self._deadline: float | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> str | None:
        self._stop.set()
        self._thread.join()
        if (
            self._lost is None
            and self._deadline is not None
            and time.monotonic() >= self._deadline
        ):
            self._lost = "reservation renewal not confirmed before lease deadline"
        return self._lost

    def _run(self) -> None:
        interval = max(0.01, min(30.0, self._lease_seconds / 3))
        started_at = time.monotonic()
        self._deadline = started_at + self._lease_seconds
        next_renewal = started_at + interval
        while True:
            wait_seconds = max(0.0, next_renewal - time.monotonic())
            if self._stop.wait(wait_seconds):
                return
            attempted_at = time.monotonic()
            next_renewal = attempted_at + interval
            try:
                renewed = self._renew(self._reservation_id, self._lease_seconds)
            except Exception as exc:
                if time.monotonic() >= self._deadline:
                    self._lost = f"renewal failed with {type(exc).__name__}"
                    return
                continue
            if not renewed:
                self._lost = "reservation expired or closed before renewal"
                return
            self._deadline = attempted_at + self._lease_seconds


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
        on_node: NodeCallback | None = None,
        reservation_lease_seconds: int | float = 60,
    ) -> None:
        if (
            isinstance(reservation_lease_seconds, bool)
            or not isinstance(reservation_lease_seconds, int | float)
            or reservation_lease_seconds <= 0
        ):
            raise ValueError("reservation_lease_seconds must be positive")
        self.store: Store = _coerce_store(store)
        self.meters = meters or [StepMeter(), DepthMeter(), WallClockMeter(), TokenMeter()]
        self.registry = registry
        self.policies = policies or []
        self.dry_run = dry_run
        self.mode = normalize_mode(mode)
        self.on_node = on_node
        self.reservation_lease_seconds = float(reservation_lease_seconds)

    def _put(self, node: Node) -> Node:
        is_new = not self.store.exists(node.id)
        self.store.put(node)
        stored = self.store.get(node.id)
        if is_new and self.on_node is not None:
            try:
                self.on_node(stored)
            except Exception as exc:
                warnings.warn(
                    f"pollard on_node callback failed with {type(exc).__name__}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return stored

    def run(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> Run:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        root = self._put(root) if not self.store.exists(root.id) else self.store.get(root.id)
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
        on_delta: DeltaCallback | None = None,
        keep_chunks: bool = False,
    ) -> Node:
        return self._call(
            NodeKind.MODEL_CALL,
            payload,
            fn=fn,
            attempt=attempt,
            on_delta=on_delta,
            keep_chunks=keep_chunks,
        )

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
        node = self._runtime._put(node)
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
        anchor = self._runtime._put(anchor)
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
        on_delta: DeltaCallback | None = None,
        keep_chunks: bool = False,
    ) -> Node:
        recorded = self._recorded_node(kind, payload, attempt, on_delta=on_delta)
        if recorded is not None:
            return recorded
        reservation = self._precheck(kind.value, payload)
        lease = self._start_reservation_lease(reservation)
        measurements = _start_measurements(self._runtime.meters)
        start = time.perf_counter()
        try:
            result = _consume_step_result(
                fn(payload),
                on_delta=on_delta,
                keep_chunks=keep_chunks,
            )
        except BaseException:
            if lease is not None:
                lease.stop()
            self._release_reservation(reservation)
            raise
        finally:
            duration = time.perf_counter() - start
            _stop_measurements(measurements)
        lease_lost = lease.stop() if lease is not None else None
        meta: dict[str, Any] = {"created_at": _now_utc(), "duration_s": duration}
        for measurement in measurements:
            meta.update(measurement.readings())
        charges = self._charges(kind.value, payload, result, meta)
        meta["charges"] = charges
        if isinstance(result, dict) and isinstance(result.get("usage"), dict):
            meta["usage"] = result["usage"]
        if lease_lost is not None:
            meta["reservation_lease"] = {"status": "lost", "detail": lease_lost}
        self._settle_reservation(reservation, charges)
        node = Node.make(
            kind=kind,
            parent=self.cursor_id,
            payload=payload,
            attempt=attempt,
            result=result,
            meta=meta,
        )
        node = self._runtime._put(node)
        self.cursor_id = node.id
        self._settle_scopes()
        if lease_lost is not None and reservation is not None:
            raise ReservationLeaseLost(
                "shared reservation lease was lost while the call was running",
                reservation,
                node.id,
            )
        return node

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
        try:
            spec = registry.get(name)
        except KeyError:
            requested = name if version is None else f"{name}@{version}"
            self._refuse_policy(
                f"unknown registered action: {requested}",
                {"tool": name, "args": args},
            )
        audit_args = spec.redact_args(args)
        blocked_payload: dict[str, IdentityValue] = {"tool": name, "args": audit_args}
        if version is not None and version != spec.version:
            self._refuse_policy(
                f"unknown registered action: {name}@{version}",
                blocked_payload,
            )
        finding = spec.validate_args(args)
        if finding is not None:
            self._refuse_policy(f"schema validation failed: {finding}", blocked_payload)
        payload: dict[str, IdentityValue] = {
            "tool": spec.name,
            "version": spec.version,
            "args": audit_args,
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
            reservation = self._precheck(NodeKind.TOOL_CALL.value, payload)
            charges: dict[str, int | float] = {"steps": 1}
            self._settle_reservation(reservation, charges)
            node = Node.make(
                kind=NodeKind.TOOL_CALL,
                parent=self.cursor_id,
                payload=payload,
                attempt=attempt,
                meta={
                    "created_at": _now_utc(),
                    "dry_run": True,
                    "charges": charges,
                },
            )
            node = self._runtime._put(node)
            self.cursor_id = node.id
            self._settle_scopes()
            return node
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
        *,
        on_delta: DeltaCallback | None = None,
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
        if self._runtime.mode == ReplayMode.HYBRID:
            record_avoided_charges(self.store, node.id, charges)
        self.cursor_id = node.id
        _reemit_chunks(node, on_delta)
        return node

    def _add_avoided(self, charges: dict[str, int | float]) -> None:
        for name, amount in charges.items():
            total = charge_to_decimal(self._avoided.get(name, 0)) + charge_to_decimal(amount)
            self._avoided[name] = float(total)

    def _precheck(
        self, kind: str, payload: dict[str, IdentityValue]
    ) -> str | None:
        estimates, approximate = self._estimates(kind, payload)
        for scope in self._budget_scopes:
            check = check_budget(
                budget=scope.budget,
                spent=spent_decimal(self.store, scope.anchor_id),
                estimates=estimates,
                exhausted=scope.exhausted,
            )
            if not check.ok:
                self._refuse(
                    check,
                    kind,
                    payload,
                    estimated=check.meter in approximate,
                )
        if kind not in {NodeKind.MODEL_CALL.value, NodeKind.TOOL_CALL.value}:
            return None
        store = self.store
        if not isinstance(store, TransactionalArbiter):
            return None
        budgets = [
            BudgetReservation(
                scope_id=scope.anchor_id,
                limits=scope.budget.limits(),
                baseline=spent_decimal(store, scope.anchor_id),
                estimates=estimates,
            )
            for scope in self._budget_scopes
        ]
        windows: list[WindowReservation] = []
        payload_any: dict[str, Any] = payload
        for meter in self._runtime.meters:
            if not isinstance(meter, WindowMeter):
                continue
            estimate = meter.precheck_estimate(kind, payload_any)
            windows.append(
                WindowReservation(
                    ledger_key=meter.ledger_key(self.root_id),
                    meter=meter.name,
                    limit=meter.limit,
                    amount=(
                        Decimal("0")
                        if estimate is None
                        else charge_to_decimal(estimate)
                    ),
                    window_seconds=meter.window_seconds,
                )
            )
        if not budgets and not windows:
            return None
        reservation_id = uuid4().hex
        arbiter_check = store._pollard_reserve(
            reservation_id,
            budgets,
            windows,
            self._runtime.reservation_lease_seconds,
        )
        if not arbiter_check.ok:
            self._refuse(
                BudgetCheck(
                    ok=False,
                    meter=arbiter_check.meter,
                    requested=arbiter_check.requested,
                    remaining=arbiter_check.remaining,
                ),
                kind,
                payload,
                estimated=arbiter_check.meter in approximate,
                reason=arbiter_check.reason,
                window_seconds=arbiter_check.window_seconds,
            )
        return reservation_id

    def _estimates(
        self,
        kind: str,
        payload: dict[str, IdentityValue],
    ) -> tuple[dict[str, Decimal], set[str]]:
        estimates: dict[str, Decimal] = {}
        approximate: set[str] = set()
        payload_any: dict[str, Any] = payload
        for meter in self._runtime.meters:
            estimate = meter.precheck_estimate(kind, payload_any)
            if estimate is not None:
                estimates[meter.name] = charge_to_decimal(estimate)
                if getattr(meter, "precheck_is_estimate", False) is True:
                    approximate.add(meter.name)
        next_depth = _depth(self.store, self.cursor_id) + 1
        estimates["depth"] = Decimal(next_depth)
        return estimates, approximate

    def _refuse(
        self,
        check: BudgetCheck,
        blocked_kind: str,
        blocked_payload: dict[str, IdentityValue],
        *,
        estimated: bool = False,
        reason: str = "budget",
        window_seconds: float | None = None,
    ) -> None:
        meter = check.meter or "unknown"
        payload: dict[str, IdentityValue] = {
            "reason": reason,
            "meter": meter,
            "requested": str(check.requested),
            "remaining": str(check.remaining),
            "blocked_kind": blocked_kind,
            "blocked_payload_digest": digest_payload(blocked_payload),
        }
        if estimated:
            payload["estimated"] = "true"
        if window_seconds is not None:
            payload["window_seconds"] = (
                int(window_seconds)
                if window_seconds.is_integer()
                else str(window_seconds)
            )
        node = Node.make(
            kind=NodeKind.REFUSAL,
            parent=self.cursor_id,
            payload=payload,
            meta={"created_at": _now_utc()},
        )
        node = self._runtime._put(node)
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
        node = self._runtime._put(node)
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

    def _settle_reservation(
        self,
        reservation_id: str | None,
        charges: dict[str, int | float],
    ) -> None:
        if reservation_id is None:
            return
        store = self.store
        if not isinstance(store, TransactionalArbiter):
            return
        store._pollard_settle(
            reservation_id,
            {name: charge_to_decimal(amount) for name, amount in charges.items()},
        )

    def _start_reservation_lease(
        self,
        reservation_id: str | None,
    ) -> _LeaseHeartbeat | None:
        if reservation_id is None:
            return None
        store = self.store
        if not isinstance(store, RenewableArbiter):
            return None
        heartbeat = _LeaseHeartbeat(
            reservation_id=reservation_id,
            lease_seconds=self._runtime.reservation_lease_seconds,
            renew=store._pollard_renew,
        )
        heartbeat.start()
        return heartbeat

    def _release_reservation(self, reservation_id: str | None) -> None:
        if reservation_id is None:
            return
        store = self.store
        if isinstance(store, TransactionalArbiter):
            store._pollard_release(reservation_id)

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


def _consume_step_result(
    value: StepResult,
    *,
    on_delta: DeltaCallback | None,
    keep_chunks: bool,
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, Iterator):
        raise TypeError("step function must return a dict or an iterator of chunk dicts")
    chunks: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("stream chunks must be dicts")
        chunk = dict(item)
        chunks.append(chunk)
        if on_delta is not None:
            on_delta(chunk)
        complete = chunk.get("result")
        delta = chunk.get("delta")
        if complete is not None:
            if not isinstance(complete, dict):
                raise TypeError("a stream chunk result must be a dict")
            result = dict(complete)
        elif delta is not None:
            if not isinstance(delta, dict):
                raise TypeError("a stream chunk delta must be a dict")
            _merge_stream_value(result, delta)
        else:
            _merge_stream_value(result, chunk)
    if keep_chunks:
        result["chunks"] = chunks
    return result


def _merge_stream_value(target: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_stream_value(current, value)
        elif isinstance(current, str) and isinstance(value, str):
            target[key] = current + value
        elif isinstance(current, list) and isinstance(value, list):
            target[key] = [*current, *value]
        else:
            target[key] = value


def _reemit_chunks(node: Node, on_delta: DeltaCallback | None) -> None:
    if on_delta is None or not isinstance(node.result, dict):
        return
    chunks = node.result.get("chunks")
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if isinstance(chunk, dict):
            on_delta(chunk)


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
