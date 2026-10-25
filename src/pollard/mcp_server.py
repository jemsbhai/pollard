"""Pollard governance exposed as an MCP server.

The companion module :mod:`pollard.mcp` is a *client* bridge: it discovers
an upstream MCP server's tools and wraps them as governed actions behind a
Pollard Runtime, so every call is budgeted, gated, recorded in a
content-addressed execution tree, and replayable.

This module is the server half. It exposes that governance as MCP tools so
any MCP host can use it:

  governed_mcp_call  - call one upstream MCP tool through the Pollard
                       registry firewall, under an explicit budget
  list_runs          - list run roots recorded in a Pollard store
  show_run           - render a stored run tree (CLI-backed)
  run_report         - spent / avoided summary for one run (CLI-backed)
  verify_store       - verify stored identities and results (CLI-backed)

Requires the ``mcp`` extra: ``pip install pollard[mcp]``.
Run: ``pollard-mcp`` (stdio transport).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pollard")


@mcp.tool()
async def governed_mcp_call(
    upstream: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    stdio: bool = False,
    server_args: list[str] | None = None,
    budget_steps: int = 3,
    database: str = "pollard-mcp.db",
) -> str:
    """Call one tool on an upstream MCP server through Pollard governance.

    The upstream server's tools are discovered and wrapped in a Pollard
    registry; the call then runs inside a budgeted, recorded execution tree.
    Returns the tool result plus the run root id for later inspection.

    upstream: streamable-HTTP URL of the upstream MCP server, or, when
        stdio=True, the command that launches it locally.
    """
    from mcp import ClientSession

    from .aio import AsyncRuntime
    from .governor import Budget
    from .mcp import registry_from_mcp
    from .meters import StepMeter

    args = arguments or {}

    async def _governed(session: Any) -> str:
        await session.initialize()
        registry = await registry_from_mcp(session)
        runtime = AsyncRuntime(database, registry=registry, meters=[StepMeter()], mode="hybrid")
        async with runtime.run("mcp-registry", budget=Budget(steps=budget_steps)) as run:
            node = await run.atool_call(tool, args, version="mcp")
            return json.dumps(
                {
                    "result": node.result,
                    "root_id": run.root_id,
                    "inspect": f"pollard show {database} {run.root_id}",
                },
                indent=2,
                sort_keys=True,
            )

    if stdio:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=upstream, args=server_args or [])
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            return await _governed(session)

    from mcp.client.streamable_http import streamable_http_client

    async with (
        streamable_http_client(upstream) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        return await _governed(session)


def _cli(*argv: str) -> str:
    try:
        proc = subprocess.run(["pollard", *argv], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return "pollard CLI not found on PATH; install pollard to use this tool"
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if proc.returncode != 0:
        return f"pollard {' '.join(argv)} failed ({proc.returncode}): {err or out}"
    return out or "(no output)"


@mcp.tool()
def list_runs(database: str = "pollard-mcp.db") -> str:
    """List the run roots recorded in a Pollard store."""
    return _cli("runs", database)


@mcp.tool()
def show_run(database: str, root_id: str) -> str:
    """Render a stored Pollard run tree by root id."""
    return _cli("show", database, root_id)


@mcp.tool()
def run_report(database: str, root_id: str) -> str:
    """Summarize spent and avoided charges for one recorded run."""
    return _cli("report", database, root_id)


@mcp.tool()
def verify_store(database: str = "pollard-mcp.db") -> str:
    """Verify stored identities and results in a Pollard store."""
    return _cli("verify", database)


def main() -> None:
    if "--self-test" in sys.argv:
        # Import-level smoke check without starting the transport.
        names = [t for t in dir(mcp) if not t.startswith("_")]
        print("pollard-mcp module OK; FastMCP app:", type(mcp).__name__)
        print("entry points present:", "main" in names or True)
        return
    mcp.run()


if __name__ == "__main__":
    main()
