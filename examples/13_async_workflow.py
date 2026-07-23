"""Govern async model and tool functions in one credential-free workflow."""

from __future__ import annotations

import asyncio
from typing import Any

from pollard import AsyncRuntime, Budget, MemoryStore


async def call_model(_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": "async result",
        "usage": {"input_tokens": 2, "output_tokens": 2},
    }


async def uppercase(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload["args"]
    assert isinstance(args, dict)
    return {
        "text": str(args["text"]).upper(),
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def run_workflow() -> None:
    runtime = AsyncRuntime(MemoryStore())
    async with runtime.run(
        "async-workflow",
        budget=Budget(steps=2, tokens=10),
    ) as run:
        model = await run.amodel_call({"model": "mock-1"}, fn=call_model)
        tool = await run.atool_call(
            "uppercase",
            {"text": model.result["text"]},
            fn=uppercase,
        )
        report = run.report()

    assert tool.result["text"] == "ASYNC RESULT"
    print(f"model={model.result['text']}")
    print(f"tool={tool.result['text']}")
    print(f"spent={report['spent']}")


def main() -> None:
    asyncio.run(run_workflow())


if __name__ == "__main__":
    main()
