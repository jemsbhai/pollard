"""Named resource scopes shared by otherwise independent runs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ._canon import IdentityValue
from .errors import IntegrityError
from .governor import Budget
from .meters import WindowMeter
from .replay import ReplayMode, replay_node_or_missing
from .store import Store
from .tree import Node, NodeKind
from .verify import verify


@dataclass(frozen=True)
class SharedBudget:
    """An additive budget bound to a name in one transactional store.

    Use a new name for a new accounting period or a changed limit. The first
    binding fixes the limits; later workers must supply the same values.
    """

    name: str
    budget: Budget

    def __post_init__(self) -> None:
        _check_name(self.name)
        if not isinstance(self.budget, Budget):
            raise TypeError("shared budget requires a Budget")
        limits = self.budget.limits()
        if not limits or "depth" in limits:
            raise ValueError("shared budgets require additive limits and cannot set depth")
        if any(not value.is_finite() for value in limits.values()):
            raise ValueError("shared budget limits must be finite")

    @property
    def scope_id(self) -> str:
        return self._node().id

    def _node(self) -> Node:
        return Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"pollard_shared_budget": 1, "name": self.name},
            result={
                "limits": {
                    key: _decimal_text(value) for key, value in sorted(self.budget.limits().items())
                }
            },
        )

    def bind(self, store: Store, *, mode: ReplayMode = ReplayMode.RECORD) -> Node:
        """Create or verify this scope's stored configuration."""
        return _bind_configuration(store, self._node(), mode=mode)


def bind_window(store: Store, meter: WindowMeter, *, mode: ReplayMode) -> Node:
    from .teams import _configuration_value

    if meter.scope is None:
        raise ValueError("window has no named scope")
    wrapped = meter._meter
    payload: dict[str, IdentityValue] = {
        "pollard_shared_window": 1, "name": meter.name, "scope": meter.scope,
    }
    node = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload=payload,
        result={
            "limit": _decimal_text(meter.limit),
            "window_seconds": str(meter.window_seconds),
            "meter": _configuration_value(wrapped),
        },
    )
    return _bind_configuration(store, node, mode=mode)


def _bind_configuration(store: Store, expected: Node, *, mode: ReplayMode) -> Node:
    if mode == ReplayMode.REPLAY:
        actual = replay_node_or_missing(store, expected)
    else:
        if not store.exists(expected.id):
            store.put(expected)
        actual = store.get(expected.id)
    if not verify(store, actual.id).ok or actual.result_digest != expected.result_digest:
        raise IntegrityError(f"shared scope configuration does not match: {expected.id}")
    return actual


def _check_name(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("scope name must be a non-empty string without surrounding whitespace")


def _decimal_text(value: Decimal) -> str:
    # Budget and window limits are Decimal; fixed notation avoids treating
    # equivalent declarations such as 10 and 10.0 as different configurations.
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
