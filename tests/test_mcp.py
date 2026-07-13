import asyncio

import pytest

from pollard import AsyncRuntime, UnsupportedSchema
from pollard.mcp import registry_from_mcp


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self) -> dict[str, object]:
        return {
            "tools": [
                {
                    "name": "search",
                    "description": "Search records.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def call_tool(self, name: str, args: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, args))
        return {
            "name": name,
            "args": args,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


def test_registry_from_mcp_builds_async_handlers() -> None:
    async def scenario() -> None:
        session = FakeSession()
        registry = await registry_from_mcp(session)
        run = AsyncRuntime(registry=registry).run("mcp")
        node = await run.atool_call("search", {"query": "pollard"})
        assert node.result["args"] == {"query": "pollard"}
        assert session.calls == [("search", {"query": "pollard"})]

    asyncio.run(scenario())


def test_registry_from_mcp_reports_unsupported_tool_schema() -> None:
    class BadSession(FakeSession):
        async def list_tools(self) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "bad_tool",
                        "description": "Bad schema.",
                        "inputSchema": {"type": "object", "patternProperties": {}},
                    }
                ]
            }

    async def scenario() -> None:
        with pytest.raises(UnsupportedSchema, match="bad_tool"):
            await registry_from_mcp(BadSession())

    asyncio.run(scenario())


def test_registry_from_mcp_can_exclude_tools() -> None:
    async def scenario() -> None:
        registry = await registry_from_mcp(FakeSession(), exclude={"search"})
        with pytest.raises(KeyError):
            registry.get("search")

    asyncio.run(scenario())


def test_registry_from_mcp_accepts_object_listing_and_model_dump_result() -> None:
    class Tool:
        def __init__(self) -> None:
            self.name = "inspect"
            self.description = "Inspect object."
            self.inputSchema = {"type": "object", "additionalProperties": True}

    class Listing:
        def __init__(self) -> None:
            self.tools = [Tool()]

    class Result:
        def model_dump(self) -> dict[str, object]:
            return {"ok": True, "usage": {"input_tokens": 0, "output_tokens": 0}}

    class Session:
        def list_tools(self) -> Listing:
            return Listing()

        def call_tool(self, name: str, args: dict[str, object]) -> Result:
            assert name == "inspect"
            assert args == {"value": "x"}
            return Result()

    async def scenario() -> None:
        registry = await registry_from_mcp(Session())
        run = AsyncRuntime(registry=registry).run("mcp-object")
        node = await run.atool_call("inspect", {"value": "x"})
        assert node.result["ok"] is True

    asyncio.run(scenario())
