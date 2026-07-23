"""Async runtime mirror."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime, timezone
from inspect import isawaitable
from pathlib import Path
from typing import Any

from ._canon import IdentityValue
from .errors import (
    ConfirmationRequired,
    PostDispatchOutcomeUnknown,
    ReservationLeaseLost,
    is_post_dispatch_outcome_unknown,
    mark_post_dispatch_outcome_unknown,
)
from .governor import Budget
from .meters import Meter
from .policy import Decision, Policy, PolicyContext
from .registry import ActionSpec, Registry
from .replay import ReplayMode
from .runtime import (
    NodeCallback,
    Run,
    RunBranch,
    Runtime,
    _BudgetScope,
    _copy_scopes,
    _deepest_non_pruned_leaf,
    _LeaseHeartbeat,
    _Measurement,
    _merge_stream_value,
    _PendingToolCall,
    _raise_primary,
    _snapshot_payload,
    _start_measurements,
    _stop_lease,
    _stop_measurements,
)
from .store import Store
from .tree import Node, NodeKind

AsyncDeltaCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
AsyncStepResult = dict[str, Any] | Iterator[dict[str, Any]] | AsyncIterator[dict[str, Any]]
AsyncStepFn = Callable[
    [dict[str, Any]],
    Awaitable[AsyncStepResult] | AsyncIterator[dict[str, Any]],
]


class AsyncRuntime(Runtime):
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
        super().__init__(
            store,
            meters=meters,
            registry=registry,
            policies=policies,
            dry_run=dry_run,
            mode=mode,
            on_node=on_node,
            reservation_lease_seconds=reservation_lease_seconds,
        )

    def run(self, label: str, *, budget: Budget | None = None, attempt: int = 0) -> AsyncRun:
        root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": label}, attempt=attempt)
        if self.mode == ReplayMode.REPLAY:
            root = self._put(root)
        else:
            root = self._put(root) if not self.store.exists(root.id) else self.store.get(root.id)
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
        stored_root = (
            self._put(root)
            if self.mode == ReplayMode.REPLAY
            else self.store.get(root.id)
        )
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
        on_delta: AsyncDeltaCallback | None = None,
        keep_chunks: bool = False,
    ) -> Node:
        return await self._acall(
            NodeKind.MODEL_CALL,
            payload,
            fn=fn,
            attempt=attempt,
            on_delta=on_delta,
            keep_chunks=keep_chunks,
        )

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
        anchor = Node.make(
            kind=NodeKind.NOTE,
            parent=self.cursor_id,
            payload=payload,
            attempt=attempt,
            meta={"created_at": _now_utc()},
        )
        if self._runtime.mode != ReplayMode.REPLAY:
            self._precheck(NodeKind.NOTE.value, payload)
        anchor = self._runtime._put(anchor)
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
        on_delta: AsyncDeltaCallback | None = None,
        keep_chunks: bool = False,
    ) -> Node:
        identity_payload = _snapshot_payload(payload)
        recorded = self._recorded_node(kind, identity_payload, attempt)
        if recorded is not None:
            await _areemit_chunks(recorded, on_delta)
            return recorded
        reservation = self._precheck(kind.value, identity_payload)
        lease: _LeaseHeartbeat | None = None
        measurements: list[_Measurement] = []
        result: dict[str, Any] | None = None
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        start = time.perf_counter()
        try:
            lease = self._start_reservation_lease(reservation)
            measurements = _start_measurements(self._runtime.meters)
            pending = fn(payload)
            if isinstance(pending, AsyncIterator):
                produced: AsyncStepResult = pending
            else:
                produced = await pending
            result = await _aconsume_step_result(
                produced,
                on_delta=on_delta,
                keep_chunks=keep_chunks,
            )
        except BaseException as exc:
            primary_error = exc
        finally:
            duration = time.perf_counter() - start
            measurement_error = _stop_measurements(measurements, primary_error)
            if primary_error is None:
                primary_error = measurement_error
            elif measurement_error is not None:
                cleanup_errors.append(measurement_error)

        if primary_error is not None:
            lease_lost, lease_error = _stop_lease(lease)
            if lease_error is not None:
                cleanup_errors.append(lease_error)
            if is_post_dispatch_outcome_unknown(primary_error):
                original_error = (
                    primary_error.error
                    if isinstance(primary_error, PostDispatchOutcomeUnknown)
                    else primary_error
                )
                cleanup_errors.extend(
                    self._record_unknown_outcome(
                        kind=kind,
                        payload=identity_payload,
                        attempt=attempt,
                        reservation=reservation,
                        error=original_error,
                        duration=duration,
                        lease_lost=lease_lost,
                    )
                )
                _raise_primary(original_error, cleanup_errors)
            if result is not None:
                cleanup_errors.extend(
                    self._record_unknown_outcome(
                        kind=kind,
                        payload=identity_payload,
                        attempt=attempt,
                        reservation=reservation,
                        error=primary_error,
                        duration=duration,
                        lease_lost=lease_lost,
                        event="call_recording_failed",
                        outcome="completed_unrecorded",
                        phase="post_result_processing",
                    )
                )
                _raise_primary(primary_error, cleanup_errors)
            try:
                self._release_reservation(reservation)
            except BaseException as exc:
                cleanup_errors.append(exc)
            _raise_primary(primary_error, cleanup_errors)

        assert result is not None
        lease_lost, lease_error = _stop_lease(lease)
        if lease_error is not None:
            cleanup_errors.extend(
                self._record_unknown_outcome(
                    kind=kind,
                    payload=identity_payload,
                    attempt=attempt,
                    reservation=reservation,
                    error=lease_error,
                    duration=duration,
                    lease_lost=lease_lost,
                    event="call_recording_failed",
                    outcome="completed_unrecorded",
                    phase="post_result_processing",
                )
            )
            _raise_primary(lease_error, cleanup_errors)
        try:
            meta: dict[str, Any] = {"created_at": _now_utc(), "duration_s": duration}
            for measurement in measurements:
                meta.update(measurement.readings())
            charges = self._charges(
                kind.value,
                identity_payload,
                result,
                meta,
                reservation=reservation,
            )
            meta["charges"] = charges
            if isinstance(result, dict) and isinstance(result.get("usage"), dict):
                meta["usage"] = result["usage"]
            if lease_lost is not None:
                meta["reservation_lease"] = {"status": "lost", "detail": lease_lost}
            node = Node.make(
                kind=kind,
                parent=self.cursor_id,
                payload=identity_payload,
                attempt=attempt,
                result=result,
                meta=meta,
            )
        except BaseException as exc:
            cleanup_errors.extend(
                self._record_unknown_outcome(
                    kind=kind,
                    payload=identity_payload,
                    attempt=attempt,
                    reservation=reservation,
                    error=exc,
                    duration=duration,
                    lease_lost=lease_lost,
                    event="call_recording_failed",
                    outcome="completed_unrecorded",
                    phase="post_result_processing",
                )
            )
            _raise_primary(exc, cleanup_errors)
        self._settle_reservation(reservation, charges)
        node = self._runtime._put(node)
        self.cursor_id = node.id
        self._settle_scopes()
        if lease_lost is not None and reservation is not None:
            raise ReservationLeaseLost(
                "shared reservation lease was lost while the call was running",
                reservation.reservation_id or "",
                node.id,
            )
        return node

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
        if self._runtime.mode == ReplayMode.REPLAY:
            recorded = self._recorded_node(NodeKind.TOOL_CALL, payload, attempt)
            assert recorded is not None
            return recorded
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


async def _aconsume_step_result(
    value: AsyncStepResult,
    *,
    on_delta: AsyncDeltaCallback | None,
    keep_chunks: bool,
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    chunks: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    received_chunk = False
    try:
        if isinstance(value, AsyncIterator):
            async for item in value:
                received_chunk = True
                await _aconsume_chunk(item, chunks, result, on_delta)
        elif isinstance(value, Iterator):
            for item in value:
                received_chunk = True
                await _aconsume_chunk(item, chunks, result, on_delta)
        else:
            raise TypeError("async step function must return a dict or a chunk iterator")
    except BaseException as error:
        if not received_chunk or is_post_dispatch_outcome_unknown(error):
            raise
        marked = mark_post_dispatch_outcome_unknown(error)
        if marked is error:
            raise
        raise marked from error
    if keep_chunks:
        result["chunks"] = chunks
    return result


async def _aconsume_chunk(
    item: dict[str, Any],
    chunks: list[dict[str, Any]],
    result: dict[str, Any],
    on_delta: AsyncDeltaCallback | None,
) -> None:
    if not isinstance(item, dict):
        raise TypeError("stream chunks must be dicts")
    chunk = dict(item)
    chunks.append(chunk)
    if on_delta is not None:
        emitted = on_delta(chunk)
        if isawaitable(emitted):
            await emitted
    complete = chunk.get("result")
    delta = chunk.get("delta")
    if complete is not None:
        if not isinstance(complete, dict):
            raise TypeError("a stream chunk result must be a dict")
        result.clear()
        result.update(complete)
    elif delta is not None:
        if not isinstance(delta, dict):
            raise TypeError("a stream chunk delta must be a dict")
        _merge_stream_value(result, delta)
    else:
        _merge_stream_value(result, chunk)


async def _areemit_chunks(node: Node, on_delta: AsyncDeltaCallback | None) -> None:
    if on_delta is None or not isinstance(node.result, dict):
        return
    chunks = node.result.get("chunks")
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        emitted = on_delta(chunk)
        if isawaitable(emitted):
            await emitted


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
