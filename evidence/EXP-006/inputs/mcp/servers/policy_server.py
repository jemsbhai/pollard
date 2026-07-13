"""Pinned household budget-policy MCP server for EXP-006C."""

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("exp-006-household-policy", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="check_household_budget",
            description=(
                "Approve a household order only when its integer total is within the limit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "total_cents": {"type": "integer"},
                    "limit_cents": {"type": "integer"},
                },
                "required": ["total_cents", "limit_cents"],
                "additionalProperties": False,
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "check_household_budget":
        raise ValueError(f"unknown tool: {name}")
    total = int(arguments["total_cents"])
    limit = int(arguments["limit_cents"])
    return {
        "approved": total <= limit,
        "limit_cents": limit,
        "margin_cents": limit - total,
        "total_cents": total,
    }


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
