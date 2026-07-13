import asyncio

import pytest

from pollard import (
    ActionSpec,
    AsyncRuntime,
    ConfirmationRequired,
    Decision,
    MemoryStore,
    Registry,
    Runtime,
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


def test_async_registered_tool_awaits_handler() -> None:
    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore(), registry=make_registry()).run("async-tool")
        node = await run.atool_call("echo", {"text": "hello"})
        assert node.result["text"] == "hello"
        assert node.payload["registry_digest"]

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
