"""Shared exact-arbiter implementation for transactional key/value stores."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from pollard._canon import IdentityValue, canonical_bytes
from pollard.arbiter import BudgetReservation, ReservationCheck, WindowReservation
from pollard.errors import IntegrityError, ReservationUncertain, SettlementUncertain
from pollard.store import _validate_for_put
from pollard.tree import Node

_SCHEMA_VERSION = 1
_SCHEMA_BUCKET = "schema"
_NODE_BUCKET = "nodes"
_BUDGET_BUCKET = "budget"
_RESERVATION_BUCKET = "reservations"
_WINDOW_BUCKET = "window-events"

T = TypeVar("T")


class KVTransaction(Protocol):
    """One serializable transaction scoped to one logical Pollard store."""

    def get(self, bucket: str, key: str) -> str | None: ...

    def items(self, bucket: str) -> list[tuple[str, str]]: ...

    def put(self, bucket: str, key: str, value: str) -> None: ...

    def delete(self, bucket: str, key: str) -> None: ...

    def now(self) -> float: ...


class TransactionalKVStore(ABC):
    """Store and exact arbiter built on backend-specific serializable writes.

    Subclasses serialize every write transaction for one ``store_id``. This is
    deliberately conservative: exact Decimal accounting and permanent retry
    tombstones are more important than maximizing write concurrency.
    """

    backend_name = "transactional store"

    def _initialize_transactional_store(self) -> None:
        def initialize(tx: KVTransaction) -> None:
            version = tx.get(_SCHEMA_BUCKET, "version")
            if version is None:
                tx.put(_SCHEMA_BUCKET, "version", str(_SCHEMA_VERSION))
            elif version != str(_SCHEMA_VERSION):
                raise IntegrityError(
                    f"unsupported {self.backend_name} schema version: {version}"
                )

        self._write_reconnecting(initialize)

    def _require_transactional_store(self) -> None:
        def require(tx: KVTransaction) -> None:
            version = tx.get(_SCHEMA_BUCKET, "version")
            if version != str(_SCHEMA_VERSION):
                label = "missing" if version is None else version
                raise IntegrityError(
                    f"unsupported {self.backend_name} schema version: {label}"
                )

        self._read(require)

    @abstractmethod
    def _read(self, callback: Callable[[KVTransaction], T]) -> T: ...

    @abstractmethod
    def _write(self, callback: Callable[[KVTransaction], T]) -> T: ...

    @abstractmethod
    def _is_connection_error(self, exc: BaseException) -> bool: ...

    @abstractmethod
    def reconnect(self) -> None: ...

    def put(self, node: Node) -> None:
        _validate_for_put(node)

        def write(tx: KVTransaction) -> None:
            if node.parent is not None and tx.get(_NODE_BUCKET, node.parent) is None:
                raise KeyError(node.parent)
            current = tx.get(_NODE_BUCKET, node.id)
            if current is None:
                tx.put(_NODE_BUCKET, node.id, _node_text(node))
                return
            existing = _node_from_text(current)
            if existing.identity_tuple() != node.identity_tuple():
                raise IntegrityError(f"node id collision for {node.id}")
            if node.result_text is None or node.result_text == existing.result_text:
                return
            conflicts = list(existing.meta.get("result_conflicts", []))
            conflict = {"result_digest": node.result_digest, "result": node.result}
            if conflict in conflicts:
                return
            conflicts.append(conflict)
            tx.put(
                _NODE_BUCKET,
                node.id,
                _node_text(
                    Node.from_storage(
                        id=existing.id,
                        parent=existing.parent,
                        kind=existing.kind,
                        attempt=existing.attempt,
                        payload_text=canonical_bytes(existing.payload).decode("utf-8"),
                        result_text=existing.result_text,
                        result_digest=existing.result_digest,
                        meta_text=_json_text(
                            {**existing.meta, "result_conflicts": conflicts}
                        ),
                    )
                ),
            )

        self._write_reconnecting(write)

    def get(self, node_id: str) -> Node:
        def read(tx: KVTransaction) -> Node:
            value = tx.get(_NODE_BUCKET, node_id)
            if value is None:
                raise KeyError(node_id)
            return _node_from_text(value)

        return self._read_reconnecting(read)

    def exists(self, node_id: str) -> bool:
        return self._read_reconnecting(
            lambda tx: tx.get(_NODE_BUCKET, node_id) is not None
        )

    def children(self, node_id: str) -> list[str]:
        def read(tx: KVTransaction) -> list[str]:
            children = [
                node
                for _key, value in tx.items(_NODE_BUCKET)
                if (node := _node_from_text(value)).parent == node_id
            ]
            return [node.id for node in sorted(children, key=lambda item: (item.kind, item.id))]

        return self._read_reconnecting(read)

    def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
        def write(tx: KVTransaction) -> None:
            value = tx.get(_NODE_BUCKET, node_id)
            if value is None:
                raise KeyError(node_id)
            node = _node_from_text(value)
            tx.put(
                _NODE_BUCKET,
                node_id,
                _node_text(
                    Node.from_storage(
                        id=node.id,
                        parent=node.parent,
                        kind=node.kind,
                        attempt=node.attempt,
                        payload_text=canonical_bytes(node.payload).decode("utf-8"),
                        result_text=node.result_text,
                        result_digest=node.result_digest,
                        meta_text=_json_text({**node.meta, **patch}),
                    )
                ),
            )

        self._write_reconnecting(write)

    def walk(self, root_id: str) -> Iterator[Node]:
        def read(tx: KVTransaction) -> list[Node]:
            nodes = {
                node.id: node
                for _key, value in tx.items(_NODE_BUCKET)
                for node in [_node_from_text(value)]
            }
            children: dict[str, list[str]] = {}
            for node in nodes.values():
                if node.parent is not None:
                    children.setdefault(node.parent, []).append(node.id)
            for child_ids in children.values():
                child_ids.sort(key=lambda item: (nodes[item].kind, item))
            ordered: list[Node] = []
            pending = [root_id]
            while pending:
                node_id = pending.pop()
                try:
                    ordered.append(nodes[node_id])
                except KeyError as exc:
                    raise KeyError(node_id) from exc
                pending.extend(reversed(children.get(node_id, [])))
            return ordered

        return iter(self._read_reconnecting(read))

    def roots(self) -> list[str]:
        def read(tx: KVTransaction) -> list[str]:
            roots = [
                node
                for _key, value in tx.items(_NODE_BUCKET)
                if (node := _node_from_text(value)).parent is None
            ]
            return [
                node.id
                for node in sorted(
                    roots,
                    key=lambda item: (str(item.payload.get("run", "")), item.id),
                )
            ]

        return self._read_reconnecting(read)

    def _pollard_reserve(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        def callback(tx: KVTransaction) -> ReservationCheck:
            return self._reserve_once(
                tx, reservation_id, budgets, windows, lease_seconds
            )
        try:
            return self._write(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            return self._write(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise ReservationUncertain(
                f"{self.backend_name} reservation outcome is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _reserve_once(
        self,
        tx: KVTransaction,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        request_text, request_digest = _reservation_request(
            budgets, windows, lease_seconds
        )
        now = tx.now()
        current_text = tx.get(_RESERVATION_BUCKET, reservation_id)
        if current_text is not None:
            current = _object(current_text, "reservation")
            if current.get("request_digest") != request_digest:
                raise IntegrityError(
                    f"reservation retry changed request: {reservation_id}"
                )
            state = _string(current, "state")
            if state != "active":
                raise IntegrityError(
                    f"reservation is already {state}: {reservation_id}"
                )
            if _float(current, "expires_at") <= now:
                raise IntegrityError(
                    f"reservation expired before retry: {reservation_id}"
                )
            return ReservationCheck(ok=True)

        budget_rows = [
            (request, meter, limit)
            for request in budgets
            for meter, limit in request.limits.items()
            if meter != "depth"
        ]
        active = [
            _object(value, "reservation")
            for _key, value in tx.items(_RESERVATION_BUCKET)
        ]
        active = [
            reservation
            for reservation in active
            if reservation.get("state") == "active"
            and _float(reservation, "expires_at") > now
        ]

        for request, meter, limit in sorted(
            budget_rows, key=lambda item: (item[0].scope_id, item[1])
        ):
            key = _compound_key(request.scope_id, meter)
            stored = tx.get(_BUDGET_BUCKET, key)
            settled = Decimal("0") if stored is None else Decimal(stored)
            baseline = request.baseline.get(meter, Decimal("0"))
            if baseline > settled:
                settled = baseline
            if stored is None or Decimal(stored) != settled:
                tx.put(_BUDGET_BUCKET, key, str(settled))
            reserved = _active_amount(active, "budget", request.scope_id, meter)
            amount = request.estimates.get(meter, Decimal("0"))
            remaining = limit - settled - reserved
            if amount > remaining:
                return ReservationCheck(
                    ok=False,
                    meter=meter,
                    requested=amount,
                    remaining=remaining,
                )

        events: list[tuple[str, dict[str, Any]]] = []
        for key, value in tx.items(_WINDOW_BUCKET):
            event = _object(value, "window event")
            if _float(event, "settled_at") <= now - _float(event, "window_seconds"):
                tx.delete(_WINDOW_BUCKET, key)
            else:
                events.append((key, event))
        for window_request in sorted(windows, key=lambda item: item.ledger_key):
            settled = sum(
                (
                    Decimal(_string(event, "amount"))
                    for _key, event in events
                    if event.get("scope_id") == window_request.ledger_key
                    and event.get("meter") == window_request.meter
                    and _float(event, "settled_at") > now - window_request.window_seconds
                ),
                Decimal("0"),
            )
            reserved = _active_amount(
                active,
                "window",
                window_request.ledger_key,
                window_request.meter,
            )
            remaining = window_request.limit - settled - reserved
            if window_request.amount > remaining:
                return ReservationCheck(
                    ok=False,
                    reason="window",
                    meter=window_request.meter,
                    requested=window_request.amount,
                    remaining=remaining,
                    window_seconds=window_request.window_seconds,
                )

        details: list[dict[str, str | float]] = []
        for request, meter, _limit in budget_rows:
            details.append(
                {
                    "kind": "budget",
                    "scope_id": request.scope_id,
                    "meter": meter,
                    "amount": str(request.estimates.get(meter, Decimal("0"))),
                }
            )
        for window_request in windows:
            details.append(
                {
                    "kind": "window",
                    "scope_id": window_request.ledger_key,
                    "meter": window_request.meter,
                    "amount": str(window_request.amount),
                    "window_seconds": window_request.window_seconds,
                }
            )
        tx.put(
            _RESERVATION_BUCKET,
            reservation_id,
            _json_text(
                {
                    "request_digest": request_digest,
                    "request": request_text,
                    "state": "active",
                    "charges_digest": None,
                    "charges": None,
                    "expires_at": now + lease_seconds,
                    "created_at": now,
                    "completed_at": None,
                    "details": details,
                }
            ),
        )
        return ReservationCheck(ok=True)

    def _pollard_settle(
        self, reservation_id: str, charges: dict[str, Decimal]
    ) -> None:
        def callback(tx: KVTransaction) -> None:
            self._settle_once(tx, reservation_id, charges)
        try:
            self._write(callback)
            return
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            self._write(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise SettlementUncertain(
                f"{self.backend_name} settlement outcome is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _settle_once(
        self,
        tx: KVTransaction,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        charges_text, charges_digest = _reservation_charges(charges)
        current_text = tx.get(_RESERVATION_BUCKET, reservation_id)
        if current_text is None:
            raise IntegrityError(f"unknown reservation: {reservation_id}")
        current = _object(current_text, "reservation")
        state = _string(current, "state")
        if state == "settled":
            if current.get("charges_digest") != charges_digest:
                raise IntegrityError(
                    f"reservation retry used different charges: {reservation_id}"
                )
            return
        if state != "active":
            raise IntegrityError(f"reservation is already {state}: {reservation_id}")
        details = current.get("details")
        if not isinstance(details, list) or not details:
            raise IntegrityError(f"reservation details are missing: {reservation_id}")
        now = tx.now()
        for index, raw_detail in enumerate(details):
            if not isinstance(raw_detail, dict):
                raise IntegrityError(f"invalid reservation details: {reservation_id}")
            detail = raw_detail
            kind = _string(detail, "kind")
            scope_id = _string(detail, "scope_id")
            meter = _string(detail, "meter")
            actual = charges.get(meter, Decimal("0"))
            if kind == "budget":
                key = _compound_key(scope_id, meter)
                stored = tx.get(_BUDGET_BUCKET, key)
                if stored is None:
                    raise IntegrityError("budget state missing during settlement")
                tx.put(_BUDGET_BUCKET, key, str(Decimal(stored) + actual))
            elif kind == "window" and actual != 0:
                tx.put(
                    _WINDOW_BUCKET,
                    _compound_key(reservation_id, str(index)),
                    _json_text(
                        {
                            "scope_id": scope_id,
                            "meter": meter,
                            "amount": str(actual),
                            "settled_at": now,
                            "window_seconds": _float(detail, "window_seconds"),
                        }
                    ),
                )
            elif kind not in {"budget", "window"}:
                raise IntegrityError(f"invalid reservation kind: {kind}")
        current.update(
            {
                "state": "settled",
                "charges_digest": charges_digest,
                "charges": charges_text,
                "completed_at": now,
            }
        )
        tx.put(_RESERVATION_BUCKET, reservation_id, _json_text(current))

    def _pollard_release(self, reservation_id: str) -> None:
        def callback(tx: KVTransaction) -> None:
            self._release_once(tx, reservation_id)
        try:
            self._write(callback)
            return
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        try:
            self.reconnect()
            self._write(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
            raise ReservationUncertain(
                f"{self.backend_name} reservation release is uncertain after reconnect",
                reservation_id,
            ) from exc

    def _release_once(self, tx: KVTransaction, reservation_id: str) -> None:
        current_text = tx.get(_RESERVATION_BUCKET, reservation_id)
        if current_text is None:
            return
        current = _object(current_text, "reservation")
        state = _string(current, "state")
        if state == "released":
            return
        if state != "active":
            raise IntegrityError(f"reservation is already {state}: {reservation_id}")
        current.update({"state": "released", "completed_at": tx.now()})
        tx.put(_RESERVATION_BUCKET, reservation_id, _json_text(current))

    def _pollard_renew(self, reservation_id: str, lease_seconds: float) -> bool:
        def renew(tx: KVTransaction) -> bool:
            current_text = tx.get(_RESERVATION_BUCKET, reservation_id)
            if current_text is None:
                return False
            current = _object(current_text, "reservation")
            now = tx.now()
            if current.get("state") != "active" or _float(current, "expires_at") <= now:
                return False
            current["expires_at"] = now + lease_seconds
            tx.put(_RESERVATION_BUCKET, reservation_id, _json_text(current))
            return True

        try:
            return self._write(renew)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        self.reconnect()
        return self._write(renew)

    def _pollard_drop_nodes(self, node_ids: set[str]) -> None:
        def drop(tx: KVTransaction) -> None:
            for node_id in node_ids:
                tx.delete(_NODE_BUCKET, node_id)

        self._write_reconnecting(drop)

    def _pollard_compact(self) -> int:
        return 0

    def _read_reconnecting(self, callback: Callable[[KVTransaction], T]) -> T:
        try:
            return self._read(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        self.reconnect()
        return self._read(callback)

    def _write_reconnecting(self, callback: Callable[[KVTransaction], T]) -> T:
        try:
            return self._write(callback)
        except BaseException as exc:
            if not self._is_connection_error(exc):
                raise
        self.reconnect()
        return self._write(callback)


def _node_text(node: Node) -> str:
    return _json_text(
        {
            "id": node.id,
            "parent": node.parent,
            "kind": node.kind,
            "attempt": node.attempt,
            "payload": canonical_bytes(node.payload).decode("utf-8"),
            "result": node.result_text,
            "result_digest": node.result_digest,
            "meta": _json_text(node.meta),
        }
    )


def _node_from_text(value: str) -> Node:
    record = _object(value, "node")
    parent = record.get("parent")
    result = record.get("result")
    result_digest = record.get("result_digest")
    if parent is not None and not isinstance(parent, str):
        raise IntegrityError("stored node parent must be a string or null")
    if result is not None and not isinstance(result, str):
        raise IntegrityError("stored node result must be JSON text or null")
    if result_digest is not None and not isinstance(result_digest, str):
        raise IntegrityError("stored node result digest must be a string or null")
    try:
        return Node.from_storage(
            id=_string(record, "id"),
            parent=parent,
            kind=_string(record, "kind"),
            attempt=_integer(record, "attempt"),
            payload_text=_string(record, "payload"),
            result_text=result,
            result_digest=result_digest,
            meta_text=_string(record, "meta"),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrityError("invalid stored node") from exc


def _active_amount(
    reservations: list[dict[str, Any]], kind: str, scope_id: str, meter: str
) -> Decimal:
    total = Decimal("0")
    for reservation in reservations:
        details = reservation.get("details")
        if not isinstance(details, list):
            raise IntegrityError("invalid reservation details")
        for detail in details:
            if not isinstance(detail, dict):
                raise IntegrityError("invalid reservation detail")
            if (
                detail.get("kind") == kind
                and detail.get("scope_id") == scope_id
                and detail.get("meter") == meter
            ):
                total += Decimal(_string(detail, "amount"))
    return total


def _reservation_request(
    budgets: list[BudgetReservation],
    windows: list[WindowReservation],
    lease_seconds: float,
) -> tuple[str, str]:
    document: IdentityValue = {
        "budgets": [
            {
                "scope_id": request.scope_id,
                "limits": {
                    name: str(value) for name, value in sorted(request.limits.items())
                },
                "baseline": {
                    name: str(value)
                    for name, value in sorted(request.baseline.items())
                },
                "estimates": {
                    name: str(value)
                    for name, value in sorted(request.estimates.items())
                },
            }
            for request in sorted(budgets, key=lambda item: item.scope_id)
        ],
        "windows": [
            {
                "ledger_key": request.ledger_key,
                "meter": request.meter,
                "limit": str(request.limit),
                "amount": str(request.amount),
                "window_seconds": str(request.window_seconds),
            }
            for request in sorted(windows, key=lambda item: item.ledger_key)
        ],
        "lease_seconds": str(lease_seconds),
    }
    encoded = canonical_bytes(document)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _reservation_charges(charges: dict[str, Decimal]) -> tuple[str, str]:
    encoded = canonical_bytes(
        {name: str(value) for name, value in sorted(charges.items())}
    )
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _compound_key(*parts: str) -> str:
    return canonical_bytes(list(parts)).decode("utf-8")


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _object(value: str, label: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise IntegrityError(f"invalid stored {label}") from exc
    if not isinstance(result, dict):
        raise IntegrityError(f"stored {label} must be an object")
    return result


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise IntegrityError(f"stored {key} must be a string")
    return result


def _integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise IntegrityError(f"stored {key} must be an integer")
    return result


def _float(value: dict[str, Any], key: str) -> float:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise IntegrityError(f"stored {key} must be a number")
    return float(result)
