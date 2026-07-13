"""Pinned integer arithmetic MCP server for EXP-006C."""

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("exp-006-household-math", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="sum_prices",
            description="Sum a list of integer prices in cents without floating-point rounding.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prices_cents": {
                        "type": "array",
                        "items": {"type": "integer"},
                    }
                },
                "required": ["prices_cents"],
                "additionalProperties": False,
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "sum_prices":
        raise ValueError(f"unknown tool: {name}")
    prices = [int(value) for value in arguments["prices_cents"]]
    return {"prices_cents": prices, "total_cents": sum(prices)}


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
