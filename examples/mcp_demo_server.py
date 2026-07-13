"""Credential-free MCP stdio server used by the Phase 5 live recipe."""

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("pollard-demo", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search",
            description="Search a deterministic local documentation index.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "search":
        raise ValueError(f"unknown tool: {name}")
    query = arguments["query"]
    return {
        "query": query,
        "matches": [
            {
                "title": "Pollard governed execution trees",
                "score": 1.0,
            }
        ],
    }


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
