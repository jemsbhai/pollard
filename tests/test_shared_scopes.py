from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from pollard import AsyncRuntime, Budget, BudgetExceeded, IntegrityError, Runtime, SQLiteStore
from pollard.arbiter import TransactionalArbiter
from pollard.meters import StepMeter, WindowMeter
from pollard.scopes import SharedBudget
from pollard.store import MemoryStore, Store


def test_independent_roots_share_one_atomic_budget(tmp_path: Path) -> None:
    path = tmp_path / "organization.db"
    shared = SharedBudget("organization/october", Budget(steps=3))

    def worker(index: int) -> bool:
        with SQLiteStore(path) as store:
            run = Runtime(store, meters=[StepMeter()], shared_budgets=[shared]).run(str(index))
            try:
                run.model_call({"worker": index}, fn=lambda _: {})
                return True
            except BudgetExceeded:
                return False

    # Initialize the file before opening concurrent clients.
    with SQLiteStore(path):
        pass
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(worker, range(8)))
    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 5


def test_named_window_spans_independent_roots(store: Store) -> None:
    if not isinstance(store, TransactionalArbiter):
        pytest.skip("named windows require shared arbitration")
    first = Runtime(store, meters=[WindowMeter("requests", 1, 60, scope="provider")])
    first.run("first").model_call({}, fn=lambda _: {})
    second = Runtime(store, meters=[WindowMeter("requests", 1, 60, scope="provider")])
    with pytest.raises(BudgetExceeded):
        second.run("second").model_call({}, fn=lambda _: pytest.fail("must not dispatch"))


def test_scope_configuration_cannot_silently_change(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "config.db") as store:
        Runtime(store, shared_budgets=[SharedBudget("quota", Budget(steps=2))]).run("first")
        with pytest.raises(IntegrityError, match="configuration"):
            Runtime(store, shared_budgets=[SharedBudget("quota", Budget(steps=3))]).run("second")
        Runtime(store, meters=[WindowMeter("requests", 2, 60, scope="api")]).run("window")
        with pytest.raises(IntegrityError, match="configuration"):
            Runtime(store, meters=[WindowMeter("requests", 3, 60, scope="api")]).run("changed")
        with pytest.raises(IntegrityError, match="configuration"):
            Runtime(store, meters=[WindowMeter("requests", 2, 90, scope="api")]).run("duration")


def test_named_scope_configuration_race_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    with SQLiteStore(path):
        pass

    def initialize(limit: int) -> int | None:
        with SQLiteStore(path) as store:
            try:
                Runtime(store, shared_budgets=[SharedBudget("one", Budget(steps=limit))]).run(
                    f"worker-{limit}"
                )
            except IntegrityError:
                return None
            return limit

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(initialize, [1, 2]))
    assert values.count(None) == 1


def test_shared_budget_and_branch_budget_both_apply(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "nested.db") as store:
        runtime = Runtime(store, shared_budgets=[SharedBudget("account", Budget(steps=2))])
        run = runtime.run("task", budget=Budget(steps=5))
        with run.branch(budget=Budget(steps=1)) as branch:
            branch.model_call({}, fn=lambda _: {})
            with pytest.raises(BudgetExceeded):
                branch.model_call({}, fn=lambda _: pytest.fail("branch limit"))
        runtime.run("another").model_call({}, fn=lambda _: {})
        with pytest.raises(BudgetExceeded):
            runtime.run("third").model_call({}, fn=lambda _: pytest.fail("account limit"))


def test_shared_scopes_support_async_and_replay(tmp_path: Path) -> None:
    shared = SharedBudget("account", Budget(steps=1))
    path = tmp_path / "async.db"

    async def record() -> None:
        with SQLiteStore(path) as store:
            runtime = AsyncRuntime(store, shared_budgets=[shared])

            async def call(payload: dict[str, object]) -> dict[str, object]:
                return {"text": "recorded"}

            await runtime.run("first").amodel_call({}, fn=call)
            with pytest.raises(BudgetExceeded):
                await runtime.run("second").amodel_call({}, fn=call)

    asyncio.run(record())
    runtime = Runtime(path, mode="replay", shared_budgets=[shared])
    node = runtime.run("first").model_call({}, fn=lambda _: pytest.fail("replay dispatched"))
    assert node.result == {"text": "recorded"}
    runtime.store.close()  # type: ignore[attr-defined]


def test_named_scopes_require_transactional_backend_and_valid_configuration() -> None:
    shared = SharedBudget("account", Budget(steps=1))
    configurations = (
        {"shared_budgets": [shared]},
        {"meters": [WindowMeter("requests", 1, 1, scope="x")]},
    )
    for kwargs in configurations:
        with pytest.raises(TypeError, match="transactional"):
            Runtime(MemoryStore(), **kwargs).run("invalid")  # type: ignore[arg-type]
    for name in ("", " leading", "trailing "):
        with pytest.raises(ValueError):
            SharedBudget(name, Budget(steps=1))
        with pytest.raises(ValueError):
            WindowMeter("requests", 1, 60, scope=name)
    for budget in (Budget(), Budget(depth=3), Budget(usd=Decimal("Infinity"))):
        with pytest.raises(ValueError):
            SharedBudget("invalid", budget)
    with pytest.raises(ValueError, match="unique"):
        Runtime(shared_budgets=[shared, shared])
    with pytest.raises(TypeError):
        Runtime(shared_budgets=["invalid"])  # type: ignore[list-item]


def test_equal_decimal_budget_declarations_match(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "decimal.db") as store:
        first = SharedBudget("cost", Budget(usd="1.00")).bind(store)
        second = SharedBudget("cost", Budget(usd="1")).bind(store)
        assert first.result_digest == second.result_digest


def test_mutating_named_window_is_rejected_before_dispatch(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "mutation.db") as store:
        meter = WindowMeter("requests", 1, 60, scope="provider")
        run = Runtime(store, meters=[meter]).run("task")
        meter.limit = Decimal("100")
        with pytest.raises(IntegrityError):
            run.model_call({}, fn=lambda _: pytest.fail("changed config dispatched"))
