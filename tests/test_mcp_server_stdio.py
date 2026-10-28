"""Exercise the actual stdio server, upstream subprocess, and inspection CLI."""

import asyncio
import json
import subprocess
import sys
import sysconfig
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_records_fresh_calls_and_inspects_them(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream.py"
    counter = tmp_path / "calls.txt"
    upstream.write_text(
        "from pathlib import Path\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "app = FastMCP('local-test')\n"
        "@app.tool()\n"
        "def echo(message: str) -> dict[str, str]:\n"
        f"    path = Path({str(counter)!r})\n"
        "    with path.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(message + '\\n')\n"
        "    return {'message': message}\n"
        "app.run()\n",
        encoding="utf-8",
    )
    database = str(tmp_path / "recordings with spaces.db")

    async def scenario() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pollard.mcp_server"],
            cwd=str(tmp_path),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "governed_mcp_call", "list_runs", "show_run", "run_report", "verify_store",
            }
            arguments = {
                "upstream": sys.executable,
                "stdio": True,
                "server_args": [str(upstream)],
                "tool": "echo",
                "arguments": {"message": "caf\u00e9"},
                "database": database,
            }
            roots = []
            for _ in range(2):
                result = await session.call_tool("governed_mcp_call", arguments)
                assert not result.isError, result
                recorded = json.loads(result.content[0].text)
                assert recorded["result"]["structuredContent"] == {"message": "caf\u00e9"}
                roots.append(recorded["root_id"])
            assert len(set(roots)) == 2

            denied = await session.call_tool(
                "governed_mcp_call", {**arguments, "budget_steps": 0}
            )
            assert denied.isError
            invalid = await session.call_tool(
                "governed_mcp_call", {**arguments, "arguments": {"wrong": "field"}}
            )
            assert invalid.isError
            assert counter.read_text(encoding="utf-8").splitlines() == ["caf\u00e9", "caf\u00e9"]

            runs = await session.call_tool("list_runs", {"database": database})
            assert not runs.isError, runs
            assert all(root in runs.content[0].text for root in roots)
            for name in ("show_run", "run_report"):
                result = await session.call_tool(name, {"database": database, "root_id": roots[0]})
                assert not result.isError, result
                assert roots[0] in result.content[0].text
            verified = await session.call_tool("verify_store", {"database": database})
            assert not verified.isError, verified
            missing = await session.call_tool(
                "show_run", {"database": database, "root_id": "missing"}
            )
            assert missing.isError

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))


def test_installed_entry_point_self_test_and_help(tmp_path: Path) -> None:
    executable = Path(sysconfig.get_path("scripts")) / (
        "pollard-mcp.exe" if sys.platform == "win32" else "pollard-mcp"
    )
    for argument in ("--self-test", "--help"):
        result = subprocess.run(
            [str(executable), argument], cwd=tmp_path, capture_output=True,
            text=True, timeout=30, check=True,
        )
        if argument == "--self-test":
            assert json.loads(result.stdout)["tools"] == [
                "governed_mcp_call", "list_runs", "run_report", "show_run", "verify_store",
            ]
        else:
            assert "--self-test" in result.stdout


def test_core_import_does_not_require_mcp_and_server_explains_missing_extra(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys
class NoMCP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.'):
            raise ModuleNotFoundError('mcp is not installed', name='mcp')
sys.meta_path.insert(0, NoMCP())
import pollard
from pollard.cli import main
assert main(['--help']) == 0
"""],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    # argparse exits successfully after the core CLI help, without importing MCP.
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys
class NoMCP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.'):
            raise ModuleNotFoundError('mcp is not installed', name='mcp')
sys.meta_path.insert(0, NoMCP())
import pollard.mcp_server
"""],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert 'pip install "pollard[mcp]"' in result.stderr
