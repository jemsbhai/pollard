import asyncio

import pytest

from pollard import (
    ActionSpec,
    AsyncRuntime,
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


async def _async_result(text: str) -> dict[str, object]:
    return {"text": text, "usage": {"input_tokens": 0, "output_tokens": 0}}
