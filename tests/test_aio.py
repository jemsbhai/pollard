import asyncio
from decimal import Decimal

import pytest

from pollard import (
    ActionSpec,
    AsyncRuntime,
    Budget,
    BudgetExceeded,
    ConfirmationRequired,
    Decision,
    MemoryStore,
    PolicyViolation,
    Registry,
    Runtime,
    SQLiteStore,
    WindowMeter,
)
from pollard.arbiter import BudgetReservation, ReservationCheck, WindowReservation
from pollard.errors import PostDispatchOutcomeUnknown, ReservationUncertain
from pollard.meters import StepMeter, TokenMeter


def make_registry(side_effects: bool = False) -> Registry:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        return {"text": args["text"], "usage": {"input_tokens": 1, "output_tokens": 1}}

    return Registry(
        [
            ActionSpec(
                "echo",
                "1",
                "Echo text.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                side_effects,
                handler,
            )
        ]
    )


class AsyncTrackingArbiter(MemoryStore):
    def __init__(self, *, release_error: BaseException | None = None) -> None:
        super().__init__()
        self.release_error = release_error
        self.released: list[str] = []
        self.settled: list[tuple[str, dict[str, Decimal]]] = []

    def _pollard_reserve(
        self,
        reservation_id: str,
        budgets: list[BudgetReservation],
        windows: list[WindowReservation],
        lease_seconds: float,
    ) -> ReservationCheck:
        del budgets, windows, lease_seconds
        return ReservationCheck(ok=True)

    def _pollard_settle(
        self,
        reservation_id: str,
        charges: dict[str, Decimal],
    ) -> None:
        self.settled.append((reservation_id, charges))

    def _pollard_release(self, reservation_id: str) -> None:
        self.released.append(reservation_id)
        if self.release_error is not None:
            raise self.release_error

    def _pollard_renew(self, reservation_id: str, lease_seconds: float) -> bool:
        del reservation_id, lease_seconds
        return True


def test_async_running_call_renews_sqlite_reservation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        path = tmp_path / "async-renewal.db"
        started = asyncio.Event()
        executed: list[str] = []

        async def slow_call(_payload: dict[str, object]) -> dict[str, bool]:
            started.set()
            await asyncio.sleep(2.2)
            executed.append("first")
            return {"ok": True}

        with SQLiteStore(path) as first_store:
            first_run = AsyncRuntime(
                first_store,
                meters=[WindowMeter("requests", 1, 60)],
                reservation_lease_seconds=1.0,
            ).run("async-renewal")
            pending = asyncio.create_task(
                first_run.amodel_call({"model": "slow"}, fn=slow_call)
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.sleep(1.4)
            with SQLiteStore(path) as second_store:
                second_run = Runtime(
                    second_store,
                    meters=[WindowMeter("requests", 1, 60)],
                    reservation_lease_seconds=1.0,
                ).run("async-renewal")
                with pytest.raises(BudgetExceeded):
                    second_run.model_call(
                        {"model": "second"},
                        attempt=1,
                        fn=lambda _payload: executed.append("second")
                        or {"ok": True},
                    )
            await asyncio.wait_for(pending, timeout=10)

        assert executed == ["first"]

    asyncio.run(scenario())


def test_async_in_memory_sqlite_keeps_reservation_until_completion() -> None:
    async def scenario() -> None:
        store = SQLiteStore(":memory:")
        started = asyncio.Event()
        executed: list[str] = []

        async def slow_call(_payload: dict[str, object]) -> dict[str, bool]:
            started.set()
            await asyncio.sleep(0.2)
            executed.append("first")
            return {"ok": True}

        first_run = AsyncRuntime(
            store,
            meters=[WindowMeter("requests", 1, 60)],
            reservation_lease_seconds=0.05,
        ).run("async-memory-renewal")
        second_run = AsyncRuntime(
            store,
            meters=[WindowMeter("requests", 1, 60)],
            reservation_lease_seconds=0.05,
        ).run("async-memory-renewal")
        pending = asyncio.create_task(
            first_run.amodel_call({"model": "slow"}, fn=slow_call)
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        with pytest.raises(BudgetExceeded):
            await second_run.amodel_call(
                {"model": "second"},
                attempt=1,
                fn=lambda _payload: _async_result(executed, "second"),
            )
        await asyncio.wait_for(pending, timeout=5)
        store.close()
        assert executed == ["first"]

    asyncio.run(scenario())


async def _async_result(target: list[str], value: str) -> dict[str, bool]:
    target.append(value)
    return {"ok": True}


def test_async_model_call_matches_sync_identity() -> None:
    sync_run = Runtime(MemoryStore()).run("parity")
    sync_node = sync_run.model_call(
        {"model": "mock-1", "messages": []},
        fn=lambda _payload: {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )

    async def scenario() -> str:
        async_run = AsyncRuntime(MemoryStore()).run("parity")

        async def fn(_payload: dict[str, object]) -> dict[str, object]:
            return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

        node = await async_run.amodel_call({"model": "mock-1", "messages": []}, fn=fn)
        return node.id

    assert asyncio.run(scenario()) == sync_node.id


def test_async_replay_hit_never_awaits_fn() -> None:
    store = MemoryStore()

    async def record() -> None:
        run = AsyncRuntime(store, mode="record").run("async-replay")

        async def fn(_payload: dict[str, object]) -> dict[str, object]:
            return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

        await run.amodel_call({"model": "mock-1"}, fn=fn)

    async def replay() -> None:
        run = AsyncRuntime(store, mode="replay").run("async-replay")

        async def fn(_payload: dict[str, object]) -> dict[str, object]:
            raise AssertionError("replay mode awaited fn")

        node = await run.amodel_call({"model": "mock-1"}, fn=fn)
        assert node.result["text"] == "ok"
        avoided = run.report()["avoided"]
        assert avoided["steps"] == 1.0
        assert avoided["tokens"] == 2.0

    asyncio.run(record())
    asyncio.run(replay())


def test_async_registered_tool_awaits_handler() -> None:
    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore(), registry=make_registry()).run("async-tool")
        node = await run.atool_call("echo", {"text": "hello"})
        assert node.result["text"] == "hello"
        assert node.payload["registry_digest"]

    asyncio.run(scenario())


def test_async_unfenced_tool_call_and_required_fn() -> None:
    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore()).run("async-unfenced")
        with pytest.raises(TypeError, match="requires fn"):
            await run.atool_call("echo", {"text": "hello"})

        async def fn(payload: dict[str, object]) -> dict[str, object]:
            return {"seen": payload, "usage": {"input_tokens": 0, "output_tokens": 0}}

        node = await run.atool_call("echo", {"text": "hello"}, fn=fn)
        assert node.result["seen"] == {"tool": "echo", "args": {"text": "hello"}}

    asyncio.run(scenario())


def test_async_registry_refuses_bad_args() -> None:
    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore(), registry=make_registry()).run("async-refuse")
        with pytest.raises(PolicyViolation) as exc_info:
            await run.atool_call("echo", {"text": 1})
        refusal = run.store.get(exc_info.value.refusal_id)
        assert "schema validation failed" in str(refusal.payload["detail"])

    asyncio.run(scenario())


def test_async_branch_and_resume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        db_path = tmp_path / "async-runs.db"
        with SQLiteStore(db_path) as store:
            run = AsyncRuntime(store).run("async-resume")
            original = run.cursor_id
            with run.branch(attempt=1) as branch:
                node = await branch.amodel_call(
                    {"model": "mock-1"},
                    fn=lambda _payload: _async_result("branch"),
                )
            assert run.cursor_id == original
            assert node.parent != original

        with SQLiteStore(db_path) as store:
            resumed = AsyncRuntime(store).resume("async-resume")
            assert resumed.cursor_id == node.id

    asyncio.run(scenario())


def test_async_policy_confirm_resumes_once() -> None:
    class ConfirmPolicy:
        def decide(self, _ctx: object) -> Decision:
            return Decision.CONFIRM

    async def scenario() -> None:
        run = AsyncRuntime(
            MemoryStore(),
            registry=make_registry(),
            policies=[ConfirmPolicy()],
        ).run("async-confirm")
        with pytest.raises(ConfirmationRequired) as exc_info:
            await run.atool_call("echo", {"text": "hello"})
        node = await run.aconfirm(exc_info.value.resume_token)
        assert node.result["text"] == "hello"
        with pytest.raises(KeyError):
            await run.aconfirm(exc_info.value.resume_token)

    asyncio.run(scenario())


def test_async_dry_run_suppresses_side_effect_handler() -> None:
    called = False

    async def handler(args: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": args["text"]}

    registry = Registry(
        [
            ActionSpec(
                "write",
                "1",
                "Write text.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                True,
                handler,
            )
        ]
    )

    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore(), registry=registry, dry_run=True).run("async-dry")
        node = await run.atool_call("write", {"text": "hello"})
        assert node.result is None
        assert node.meta["dry_run"] is True

    asyncio.run(scenario())
    assert not called


def test_sync_runtime_rejects_async_registered_handler() -> None:
    run = Runtime(MemoryStore(), registry=make_registry()).run("sync-async")
    with pytest.raises(TypeError, match="AsyncRuntime"):
        run.tool_call("echo", {"text": "hello"})


def test_async_stream_forwards_and_replays_chunks() -> None:
    store = MemoryStore()

    async def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        yield {"delta": {"text": "async "}}
        yield {
            "result": {
                "text": "async stream",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        }

    async def scenario() -> None:
        live_seen: list[dict[str, object]] = []

        async def on_delta(chunk: dict[str, object]) -> None:
            live_seen.append(chunk)

        live = AsyncRuntime(store, mode="record").run("async-stream")
        node = await live.amodel_call(
            {"model": "mock-1"},
            fn=stream,
            on_delta=on_delta,
            keep_chunks=True,
        )
        assert node.result["text"] == "async stream"
        assert node.meta["charges"]["steps"] == 1
        assert node.meta["charges"]["tokens"] == 3

        replay_seen: list[dict[str, object]] = []
        replay = AsyncRuntime(store, mode="replay").run("async-stream")
        replayed = await replay.amodel_call(
            {"model": "mock-1"},
            fn=stream,
            on_delta=replay_seen.append,
        )
        assert live_seen == replay_seen == replayed.result["chunks"]

    asyncio.run(scenario())


def test_async_window_reservation_settles_in_sqlite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        with SQLiteStore(tmp_path / "async-window.db") as store:
            run = AsyncRuntime(
                store,
                meters=[WindowMeter("requests", 1, 60)],
            ).run("async-window")
            await run.amodel_call(
                {"model": "mock", "index": 1},
                fn=lambda _payload: _async_result("first"),
            )
            with pytest.raises(BudgetExceeded) as exc_info:
                await run.amodel_call(
                    {"model": "mock", "index": 2},
                    fn=lambda _payload: _async_result("second"),
                )
            refusal = store.get(exc_info.value.refusal_id)
            assert refusal.payload["reason"] == "window"

    asyncio.run(scenario())


def test_async_callable_mutation_cannot_change_recorded_payload() -> None:
    async def scenario() -> None:
        payload = {
            "model": "mock-1",
            "messages": [{"role": "user", "content": "original"}],
        }
        expected = {
            "model": "mock-1",
            "messages": [{"role": "user", "content": "original"}],
        }

        async def mutate(received: dict[str, object]) -> dict[str, object]:
            received.clear()
            received["model"] = "mutated"
            return {"text": "ok"}

        run = AsyncRuntime(MemoryStore()).run("async-payload-snapshot")
        node = await run.amodel_call(payload, fn=mutate)

        assert payload == {"model": "mutated"}
        assert node.payload == expected

    asyncio.run(scenario())


def test_async_stream_callback_failure_after_chunk_settles_estimate() -> None:
    async def scenario() -> None:
        store = AsyncTrackingArbiter()
        run = AsyncRuntime(store, meters=[StepMeter()]).run(
            "async-stream-callback-failure",
            budget=Budget(steps=2),
        )
        callback_error = RuntimeError("async consumer stopped")

        async def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
            yield {"delta": {"text": "started"}}

        async def fail_callback(_chunk: dict[str, object]) -> None:
            raise callback_error

        with pytest.raises(RuntimeError, match="async consumer stopped") as raised:
            await run.amodel_call(
                {"model": "mock"},
                fn=stream,
                on_delta=fail_callback,
            )

        assert raised.value is callback_error
        assert store.released == []
        assert store.settled[0][1] == {"steps": Decimal("1")}
        assert run.cursor.payload["event"] == "call_outcome_unknown"

    asyncio.run(scenario())


def test_async_stream_cancellation_after_chunk_settles_estimate() -> None:
    async def scenario() -> None:
        store = AsyncTrackingArbiter()
        run = AsyncRuntime(store, meters=[StepMeter()]).run(
            "async-stream-cancellation",
            budget=Budget(steps=2),
        )
        provider_error = asyncio.CancelledError("cancelled active stream")

        async def stream(_payload: dict[str, object]):  # type: ignore[no-untyped-def]
            yield {"delta": {"text": "started"}}
            raise provider_error

        with pytest.raises(asyncio.CancelledError) as raised:
            await run.amodel_call({"model": "mock"}, fn=stream)

        assert raised.value is provider_error
        assert store.released == []
        assert store.settled[0][1] == {"steps": Decimal("1")}
        assert run.cursor.payload["event"] == "call_outcome_unknown"
        assert run.cursor.meta["failure"]["error_type"] == "CancelledError"

    asyncio.run(scenario())


def test_async_missing_usage_settles_conservative_precheck_estimate() -> None:
    class EstimateSeven:
        def estimate_input_tokens(self, _payload: dict[str, object]) -> int:
            return 7

    async def scenario() -> None:
        store = AsyncTrackingArbiter()
        run = AsyncRuntime(
            store,
            meters=[StepMeter(), TokenMeter(EstimateSeven(), reserved_output_tokens=5)],
        ).run("async-missing-usage", budget=Budget(steps=2, tokens=20))

        async def result_without_usage(
            _payload: dict[str, object],
        ) -> dict[str, str]:
            return {"text": "completed without usage"}

        with pytest.warns(UserWarning, match="no compatible usage"):
            node = await run.amodel_call(
                {"model": "mock"},
                fn=result_without_usage,
            )

        assert node.meta["charges"] == {"steps": 1, "tokens": 12}
        assert node.meta["accounting_fallbacks"]["tokens"]["source"] == (
            "precheck_estimate"
        )
        assert store.settled[0][1] == {
            "steps": Decimal("1"),
            "tokens": Decimal("12"),
        }

    asyncio.run(scenario())


def test_async_release_uncertainty_does_not_mask_callable_error() -> None:
    async def scenario() -> None:
        release_error = ReservationUncertain("release uncertain", "reservation")
        store = AsyncTrackingArbiter(release_error=release_error)
        run = AsyncRuntime(store, meters=[StepMeter()]).run(
            "async-release-primary",
            budget=Budget(steps=2),
        )
        provider_error = RuntimeError("async raw provider detail")

        async def fail(_payload: dict[str, object]) -> dict[str, object]:
            raise provider_error

        with pytest.raises(RuntimeError, match="async raw provider detail") as raised:
            await run.amodel_call({"model": "mock"}, fn=fail)

        assert raised.value is provider_error
        assert len(store.released) == 1
        assert raised.value.__cause__ is release_error

    asyncio.run(scenario())


def test_async_meter_exit_failure_after_result_settles_estimate() -> None:
    class FailingMeasurement:
        def __enter__(self) -> "FailingMeasurement":
            return self

        def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            raise RuntimeError("async meter exit failed")

        def readings(self) -> dict[str, float]:
            return {}

    class FailingMeter:
        name = "failing"

        def measure(self) -> FailingMeasurement:
            return FailingMeasurement()

        def charge(
            self,
            _kind: str,
            _payload: dict[str, object],
            _result: object,
            _meta: dict[str, object],
        ) -> int:
            return 0

        def precheck_estimate(
            self,
            _kind: str,
            _payload: dict[str, object],
        ) -> None:
            return None

    async def scenario() -> None:
        store = AsyncTrackingArbiter()
        run = AsyncRuntime(store, meters=[StepMeter(), FailingMeter()]).run(
            "async-post-result-meter-failure",
            budget=Budget(steps=2),
        )

        with pytest.raises(RuntimeError, match="async meter exit failed"):
            await run.amodel_call(
                {"model": "mock"},
                fn=lambda _payload: _async_result("completed"),
            )

        assert store.released == []
        assert store.settled[0][1] == {"steps": Decimal("1")}
        assert run.cursor.payload["event"] == "call_recording_failed"
        assert run.cursor.meta["failure"]["outcome"] == "completed_unrecorded"

    asyncio.run(scenario())


def test_async_post_dispatch_unknown_settles_estimates_and_records_note() -> None:
    class EstimateThree:
        def estimate_input_tokens(self, _payload: dict[str, object]) -> int:
            return 3

    async def scenario() -> None:
        store = AsyncTrackingArbiter()
        run = AsyncRuntime(
            store,
            meters=[StepMeter(), TokenMeter(EstimateThree(), reserved_output_tokens=5)],
        ).run(
            "async-unknown-outcome",
            budget=Budget(steps=2, tokens=100),
        )
        provider_error = RuntimeError("async secret provider detail")

        async def fail(_payload: dict[str, object]) -> dict[str, object]:
            raise PostDispatchOutcomeUnknown(provider_error)

        with pytest.raises(RuntimeError, match="async secret provider detail") as raised:
            await run.amodel_call(
                {"model": "mock", "prompt": "async private prompt"},
                fn=fail,
            )

        assert raised.value is provider_error
        assert store.released == []
        assert store.settled[0][1] == {
            "steps": Decimal("1"),
            "tokens": Decimal("8"),
        }
        failure = run.cursor
        assert failure.kind == "note"
        assert failure.meta["charges"] == {"steps": 1, "tokens": 8}
        assert "async secret provider detail" not in str(failure)
        assert "async private prompt" not in str(failure)

    asyncio.run(scenario())


async def _async_result(text: str) -> dict[str, object]:
    return {"text": text, "usage": {"input_tokens": 0, "output_tokens": 0}}
