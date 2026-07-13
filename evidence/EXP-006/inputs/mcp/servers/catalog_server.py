"""Pinned household catalog MCP server for EXP-006C."""

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("exp-006-household-catalog", version="1.0.0")

CATALOG = [
    {"name": "dish soap", "price_cents": 349},
    {"name": "laundry detergent", "price_cents": 899},
    {"name": "paper towels", "price_cents": 699},
    {"name": "premium air freshener", "price_cents": 1299},
    {"name": "sponges", "price_cents": 299},
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="lookup_household_items",
            description="Search the pinned household catalog for planning candidates.",
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
    if name != "lookup_household_items":
        raise ValueError(f"unknown tool: {name}")
    return {"query": str(arguments["query"]), "items": CATALOG}


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
