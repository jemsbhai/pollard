"""Budget and charge accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .meters import ChargeAmount
from .store import Store


@dataclass(frozen=True)
class Budget:
    usd: str | Decimal | None = None
    tokens: int | None = None
    depth: int | None = None
    seconds: int | float | Decimal | None = None
    steps: int | None = None
    extra: dict[str, int | float | Decimal] | None = None

    def limits(self) -> dict[str, Decimal]:
        limits: dict[str, Decimal] = {}
        _add_limit(limits, "usd", self.usd)
        _add_limit(limits, "tokens", self.tokens)
        _add_limit(limits, "depth", self.depth)
        _add_limit(limits, "seconds", self.seconds)
        _add_limit(limits, "steps", self.steps)
        if self.extra is not None:
            for name, value in self.extra.items():
                _add_limit(limits, name, value)
        return limits


@dataclass(frozen=True)
class BudgetCheck:
    ok: bool
    meter: str | None = None
    requested: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")


def charge_to_decimal(value: ChargeAmount) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("charge values cannot be bool")
    return Decimal(str(value))


def charge_to_json(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def recompute_charges(store: Store, root_id: str) -> dict[str, float]:
    totals: dict[str, Decimal] = {}
    for node in store.walk(root_id):
        charges = node.meta.get("charges", {})
        if not isinstance(charges, dict):
            continue
        for name, amount in charges.items():
            if isinstance(name, str) and isinstance(amount, int | float):
                totals[name] = totals.get(name, Decimal("0")) + charge_to_decimal(amount)
    return {name: float(value) for name, value in sorted(totals.items())}


def spent_decimal(store: Store, anchor_id: str) -> dict[str, Decimal]:
    return {
        name: Decimal(str(value))
        for name, value in recompute_charges(store, anchor_id).items()
    }


def check_budget(
    *,
    budget: Budget | None,
    spent: dict[str, Decimal],
    estimates: dict[str, Decimal],
    exhausted: set[str],
) -> BudgetCheck:
    if budget is None:
        return BudgetCheck(ok=True)
    limits = budget.limits()
    for meter in sorted(exhausted):
        if meter in limits:
            limit = limits[meter]
            already = spent.get(meter, Decimal("0"))
            return BudgetCheck(
                ok=False,
                meter=meter,
                requested=Decimal("0"),
                remaining=limit - already,
            )
    for meter, requested in estimates.items():
        limit_value = limits.get(meter)
        if limit_value is None:
            continue
        already = spent.get(meter, Decimal("0"))
        remaining = limit_value - already
        if remaining - requested < 0:
            return BudgetCheck(ok=False, meter=meter, requested=requested, remaining=remaining)
    return BudgetCheck(ok=True)


def exhausted_after_settle(
    *,
    budget: Budget | None,
    spent: dict[str, Decimal],
) -> set[str]:
    exhausted: set[str] = set()
    if budget is None:
        return exhausted
    for meter, limit in budget.limits().items():
        if spent.get(meter, Decimal("0")) > limit:
            exhausted.add(meter)
    return exhausted


def _add_limit(
    limits: dict[str, Decimal],
    name: str,
    value: str | int | float | Decimal | None,
) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        raise TypeError(f"{name} budget cannot be bool")
    decimal = Decimal(str(value))
    if decimal < 0:
        raise ValueError(f"{name} budget cannot be negative")
    limits[name] = decimal
