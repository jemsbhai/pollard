"""Transactional reserve/settle contracts for shared governance."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class BudgetReservation:
    scope_id: str
    limits: dict[str, Decimal]
    baseline: dict[str, Decimal]
    estimates: dict[str, Decimal]


@dataclass(frozen=True)
class WindowReservation:
    ledger_key: str
    meter: str
    limit: Decimal
    amount: Decimal
    window_seconds: float


@dataclass(frozen=True)
class ReservationCheck:
    ok: bool
    reason: str = "budget"
    meter: str | None = None
    requested: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    window_seconds: float | None = None


@runtime_checkable
class TransactionalArbiter(Protocol):
    def _pollard_reserve(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck: ...

    def _pollard_settle(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None: ...

    def _pollard_release(self, reservation_id: str) -> None: ...
