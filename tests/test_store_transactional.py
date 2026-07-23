from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import RLock
from typing import TypeVar

import pytest

from pollard.arbiter import BudgetReservation, TransactionalArbiter, WindowReservation
from pollard.errors import IntegrityError, ReservationUncertain, SettlementUncertain
from pollard.stores._transactional import KVTransaction, TransactionalKVStore
from pollard.tree import Node, NodeKind

T = TypeVar("T")


class _ConnectionLost(Exception):
    pass


class _FakeStore(TransactionalKVStore):
    backend_name = "fake"

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.clock = 1_000.0
        self.failures: list[str] = []
        self.reconnects = 0
        self.lock = RLock()
        self._initialize_transactional_store()

    def _read(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._run(callback, write=False)

    def _write(self, callback: Callable[[KVTransaction], T]) -> T:
        return self._run(callback, write=True)

    def _run(self, callback: Callable[[KVTransaction], T], *, write: bool) -> T:
        with self.lock:
            if self.failures and self.failures[0] == "before":
                self.failures.pop(0)
                raise _ConnectionLost
            copy = deepcopy(self.data)
            result = callback(_FakeTransaction(copy, self.clock))
            if write:
                self.data = copy
            if self.failures and self.failures[0] == "after":
                self.failures.pop(0)
                raise _ConnectionLost
            return result

    def _is_connection_error(self, exc: BaseException) -> bool:
        return isinstance(exc, _ConnectionLost)

    def reconnect(self) -> None:
        self.reconnects += 1


class _FakeTransaction:
    def __init__(self, data: dict[str, dict[str, str]], now: float) -> None:
        self.data = data
        self.timestamp = now

    def get(self, bucket: str, key: str) -> str | None:
        return self.data.get(bucket, {}).get(key)

    def items(self, bucket: str) -> list[tuple[str, str]]:
        return sorted(self.data.get(bucket, {}).items())

    def put(self, bucket: str, key: str, value: str) -> None:
        self.data.setdefault(bucket, {})[key] = value

    def delete(self, bucket: str, key: str) -> None:
        self.data.get(bucket, {}).pop(key, None)

    def now(self) -> float:
        return self.timestamp


def _budget(scope: str = "scope", limit: str = "2") -> BudgetReservation:
    return BudgetReservation(
        scope_id=scope,
        limits={"steps": Decimal(limit)},
        baseline={},
        estimates={"steps": Decimal("1")},
    )


def test_transactional_base_implements_store_and_maintenance_contract() -> None:
    store = _FakeStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "fake"})
    child = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"text": "one"},
        result={"ok": True},
    )
    store.put(root)
    store.put(child)
    store.update_meta(child.id, {"reviewed": True})

    assert store.get(child.id).meta == {"reviewed": True}
    assert store.exists(root.id)
    assert store.children(root.id) == [child.id]
    assert [node.id for node in store.walk(root.id)] == [root.id, child.id]
    assert store.roots() == [root.id]
    assert store._pollard_compact() == 0
    store._pollard_drop_nodes({child.id})
    assert not store.exists(child.id)


def test_transactional_base_preserves_first_result_and_rejects_identity_collision() -> None:
    store = _FakeStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "fake"})
    store.put(root)
    first = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "m"},
        result={"text": "first"},
    )
    second = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "m"},
        result={"text": "second"},
    )
    store.put(first)
    store.put(second)
    assert store.get(first.id).result == {"text": "first"}
    assert store.get(first.id).meta["result_conflicts"][0]["result"] == {
        "text": "second"
    }
    with pytest.raises(KeyError):
        store.put(Node.make(kind=NodeKind.NOTE, parent="0" * 64, payload={}))


def test_exact_budget_is_atomic_and_duplicate_settlement_is_idempotent() -> None:
    store = _FakeStore()
    assert isinstance(store, TransactionalArbiter)

    def reserve(index: int) -> bool:
        return store._pollard_reserve(str(index), [_budget()], [], 60).ok

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(reserve, range(8)))
    assert outcomes.count(True) == 2
    assert outcomes.count(False) == 6

    store._pollard_settle("0", {"steps": Decimal("1")})
    store._pollard_settle("0", {"steps": Decimal("1")})
    with pytest.raises(IntegrityError, match="different charges"):
        store._pollard_settle("0", {"steps": Decimal("2")})


def test_reservation_retry_release_and_expiry_semantics() -> None:
    store = _FakeStore()
    assert store._pollard_reserve("same", [_budget()], [], 10).ok
    assert store._pollard_reserve("same", [_budget()], [], 10).ok
    with pytest.raises(IntegrityError, match="changed request"):
        store._pollard_reserve("same", [_budget(limit="3")], [], 10)
    store._pollard_release("same")
    store._pollard_release("same")
    with pytest.raises(IntegrityError, match="already released"):
        store._pollard_reserve("same", [_budget()], [], 10)

    assert store._pollard_reserve("expired", [_budget()], [], 1).ok
    store.clock += 2
    assert not store._pollard_renew("expired", 10)
    with pytest.raises(IntegrityError, match="expired before retry"):
        store._pollard_reserve("expired", [_budget()], [], 1)


def test_window_events_expire_and_settle_at_transaction_time() -> None:
    store = _FakeStore()
    request = WindowReservation(
        ledger_key="window",
        meter="requests",
        limit=Decimal("1"),
        amount=Decimal("1"),
        window_seconds=5,
    )
    assert store._pollard_reserve("first", [], [request], 60).ok
    store._pollard_settle("first", {"requests": Decimal("1")})
    assert not store._pollard_reserve("blocked", [], [request], 60).ok
    store.clock += 6
    assert store._pollard_reserve("after", [], [request], 60).ok


def test_ambiguous_reserve_and_settle_are_recovered_by_tombstones() -> None:
    store = _FakeStore()
    store.failures = ["after"]
    assert store._pollard_reserve("reserve", [_budget()], [], 60).ok
    assert store.reconnects == 1

    store.failures = ["after"]
    store._pollard_settle("reserve", {"steps": Decimal("1")})
    assert store.reconnects == 2


def test_persistent_connection_loss_raises_explicit_uncertainty() -> None:
    store = _FakeStore()
    store.failures = ["before", "before"]
    with pytest.raises(ReservationUncertain) as reserve:
        store._pollard_reserve("reserve", [_budget()], [], 60)
    assert reserve.value.reservation_id == "reserve"

    store.failures = []
    assert store._pollard_reserve("settle", [_budget()], [], 60).ok
    store.failures = ["before", "before"]
    with pytest.raises(SettlementUncertain) as settle:
        store._pollard_settle("settle", {"steps": Decimal("1")})
    assert settle.value.reservation_id == "settle"
