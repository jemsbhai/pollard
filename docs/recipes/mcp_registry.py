"""Call one MCP tool through Pollard's registry firewall end to end."""

import argparse
import asyncio
import json
from typing import Any

from pollard import AsyncRuntime, Budget
from pollard.mcp import registry_from_mcp


async def run(url: str, tool: str, args: dict[str, Any]) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        streamable_http_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        registry = await registry_from_mcp(session)
        runtime = AsyncRuntime("mcp-registry.db", registry=registry, mode="hybrid")
        async with runtime.run("mcp-registry", budget=Budget(steps=3)) as governed:
            node = await governed.atool_call(tool, args, version="mcp")
            print(json.dumps(node.result, indent=2, sort_keys=True))
            print("root:", governed.root_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("tool")
    parser.add_argument("args", help="JSON object of tool arguments")
    parsed = parser.parse_args()
    arguments = json.loads(parsed.args)
    if not isinstance(arguments, dict):
        raise TypeError("args must decode to a JSON object")
    asyncio.run(run(parsed.url, parsed.tool, arguments))


if __name__ == "__main__":
    main()
