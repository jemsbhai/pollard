"""Call one MCP tool through Pollard's registry firewall end to end."""

import argparse
import asyncio
import json
from typing import Any

from pollard import AsyncRuntime, Budget
from pollard.mcp import registry_from_mcp


async def _run_session(session: Any, tool: str, args: dict[str, Any]) -> None:
    await session.initialize()
    registry = await registry_from_mcp(session)
    runtime = AsyncRuntime("mcp-registry.db", registry=registry, mode="hybrid")
    async with runtime.run("mcp-registry", budget=Budget(steps=3)) as governed:
        node = await governed.atool_call(tool, args, version="mcp")
        print(json.dumps(node.result, indent=2, sort_keys=True))
        print("root:", governed.root_id)


async def run(
    endpoint: str,
    tool: str,
    args: dict[str, Any],
    *,
    stdio: bool = False,
    server_args: list[str] | None = None,
) -> None:
    from mcp import ClientSession

    if stdio:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(command=endpoint, args=server_args or [])
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await _run_session(session, tool, args)
        return

    from mcp.client.streamable_http import streamable_http_client

    async with (
        streamable_http_client(endpoint) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await _run_session(session, tool, args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="treat endpoint as a local stdio server command instead of an HTTP URL",
    )
    parser.add_argument(
        "--server-arg",
        action="append",
        default=[],
        help="argument passed to the stdio server command; repeat as needed",
    )
    parser.add_argument("endpoint", help="Streamable HTTP URL or stdio server command")
    parser.add_argument("tool")
    parser.add_argument("args", help="JSON object of tool arguments")
    parsed = parser.parse_args()
    arguments = json.loads(parsed.args)
    if not isinstance(arguments, dict):
        raise TypeError("args must decode to a JSON object")
    asyncio.run(
        run(
            parsed.endpoint,
            parsed.tool,
            arguments,
            stdio=parsed.stdio,
            server_args=parsed.server_arg,
        )
    )


if __name__ == "__main__":
    main()
