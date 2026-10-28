import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pollard import BudgetExceeded, Runtime, SQLiteStore, verify
from pollard import mcp_server as server
from pollard.errors import PolicyViolation

TOOLS = {"governed_mcp_call", "list_runs", "run_report", "show_run", "verify_store"}


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"opened": [], "closed": [], "calls": [], "error": None}

    class Session:
        def __init__(self, endpoint: str, _write: Any) -> None:
            self.endpoint = endpoint

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            state["closed"].append(self.endpoint)

        async def initialize(self) -> None:
            pass

        async def list_tools(self) -> dict[str, Any]:
            name = "lookup" if self.endpoint == "different-schema" else "search"
            return {
                "tools": [{
                    "name": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }]
            }

        async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            state["calls"].append((self.endpoint, name, args))
            if state["error"] is not None:
                raise state["error"]
            return {"origin": self.endpoint, "query": args["query"], "call": len(state["calls"])}

    @asynccontextmanager
    async def stdio(params: Any) -> AsyncIterator[tuple[str, None]]:
        state["opened"].append((params.command, params.args))
        yield params.command, None

    @asynccontextmanager
    async def http(endpoint: str) -> AsyncIterator[tuple[str, None, None]]:
        state["opened"].append((endpoint, None))
        yield endpoint, None, None

    monkeypatch.setattr("mcp.ClientSession", Session)
    monkeypatch.setattr("mcp.client.stdio.stdio_client", stdio)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", http)
    return state


@pytest.fixture
def opened_stores(monkeypatch: pytest.MonkeyPatch) -> list[SQLiteStore]:
    stores: list[SQLiteStore] = []

    class TrackedStore(SQLiteStore):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            stores.append(self)

    monkeypatch.setattr("pollard.stores.SQLiteStore", TrackedStore)
    return stores


def assert_closed(stores: list[SQLiteStore]) -> None:
    assert stores
    for store in stores:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.roots()


@pytest.mark.parametrize("stdio", [False, True])
def test_each_call_records_a_fresh_independent_run(
    upstream: dict[str, Any], opened_stores: list[SQLiteStore], tmp_path: Path, stdio: bool
) -> None:
    database = tmp_path / "governed.db"

    async def scenario() -> list[dict[str, Any]]:
        results = []
        for endpoint in ["server-A", "server-A", "server-B", "server-A", "different-schema"]:
            results.append(json.loads(await server.governed_mcp_call(
                endpoint,
                "lookup" if endpoint == "different-schema" else "search",
                {"query": "same arguments"},
                stdio=stdio,
                server_args=["--fixture"] if stdio else None,
                database=str(database),
            )))
        return results

    results = asyncio.run(scenario())
    assert len(upstream["calls"]) == 5
    assert [result["result"]["call"] for result in results] == list(range(1, 6))
    assert [result["result"]["origin"] for result in results] == [
        "server-A", "server-A", "server-B", "server-A", "different-schema"
    ]
    roots = {result["root_id"] for result in results}
    assert len(roots) == 5
    assert len(upstream["closed"]) == 5
    if stdio:
        assert all(args == ["--fixture"] for _, args in upstream["opened"])
    assert_closed(opened_stores)
    with SQLiteStore(database) as store:
        assert set(store.roots()) == roots
        for root in roots:
            assert verify(store, root).ok
            assert Runtime(store).resume(store.get(root).payload["run"]).report()["spent"] == {
                "steps": 1.0
            }


@pytest.mark.parametrize("budget", [-1, True, 1.5, "3", None])
def test_invalid_budget_never_opens_upstream_or_database(
    upstream: dict[str, Any], tmp_path: Path, budget: Any
) -> None:
    database = tmp_path / "invalid.db"
    with pytest.raises((TypeError, ValueError), match=r"(budget|steps)"):
        asyncio.run(server.governed_mcp_call(
            "server", "search", {"query": "x"}, budget_steps=budget, database=str(database)
        ))
    assert upstream["opened"] == []
    assert not database.exists()


def test_zero_budget_refuses_tool_and_closes_store(
    upstream: dict[str, Any], opened_stores: list[SQLiteStore], tmp_path: Path
) -> None:
    with pytest.raises(BudgetExceeded):
        asyncio.run(server.governed_mcp_call(
            "server", "search", {"query": "x"}, budget_steps=0, database=str(tmp_path / "zero.db")
        ))
    assert upstream["calls"] == []
    assert upstream["closed"] == ["server"]
    assert_closed(opened_stores)


@pytest.mark.parametrize("tool,args", [("unknown", {}), ("search", {"query": 4})])
def test_registry_refusal_never_dispatches_and_closes_store(
    upstream: dict[str, Any], opened_stores: list[SQLiteStore], tmp_path: Path,
    tool: str, args: dict[str, Any],
) -> None:
    with pytest.raises(PolicyViolation):
        asyncio.run(server.governed_mcp_call(
            "server", tool, args, database=str(tmp_path / "refused.db")
        ))
    assert upstream["calls"] == []
    assert_closed(opened_stores)


@pytest.mark.parametrize("error", [RuntimeError("upstream failed"), asyncio.CancelledError()])
def test_dispatch_error_or_cancellation_closes_store_and_transport(
    upstream: dict[str, Any], opened_stores: list[SQLiteStore], tmp_path: Path, error: BaseException
) -> None:
    upstream["error"] = error
    with pytest.raises(type(error)):
        asyncio.run(server.governed_mcp_call(
            "server", "search", {"query": "x"}, database=str(tmp_path / "failed.db")
        ))
    assert len(upstream["calls"]) == 1
    assert upstream["closed"] == ["server"]
    assert_closed(opened_stores)


def test_mcp_handshake_discovery_and_governed_call(
    upstream: dict[str, Any], tmp_path: Path
) -> None:
    async def scenario() -> None:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == TOOLS
            result = await session.call_tool("governed_mcp_call", {
                "upstream": "server", "tool": "search", "arguments": {"query": "hello"},
                "database": str(tmp_path / "protocol.db"),
            })
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["result"]["query"] == "hello"
            assert payload["root_id"]
    asyncio.run(scenario())
    assert len(upstream["calls"]) == 1


@pytest.mark.parametrize("budget", [-1, True, 1.5, "3", None])
def test_mcp_invalid_budget_refuses_before_upstream(
    upstream: dict[str, Any], tmp_path: Path, budget: Any
) -> None:
    database = tmp_path / "invalid-protocol.db"

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            result = await session.call_tool("governed_mcp_call", {
                "upstream": "server", "tool": "search", "arguments": {"query": "hello"},
                "budget_steps": budget, "database": str(database),
            })
            assert result.isError
    asyncio.run(scenario())
    assert upstream["opened"] == []
    assert not database.exists()


def test_blocked_inspection_does_not_block_other_mcp_calls(
    monkeypatch: pytest.MonkeyPatch, upstream: dict[str, Any], tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def inspection(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        entered.set()
        try:
            if not release.wait(timeout=10):
                raise RuntimeError("inspection release was never signaled")
            return subprocess.CompletedProcess([], 0, stdout='{"runs": []}', stderr="")
        finally:
            completed.set()

    monkeypatch.setattr(server.subprocess, "run", inspection)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            pending = asyncio.create_task(session.call_tool("list_runs", {}))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                result = await session.call_tool("governed_mcp_call", {
                    "upstream": "server", "tool": "search", "arguments": {"query": "hello"},
                    "database": str(tmp_path / "concurrent.db"),
                })
                assert not result.isError
                assert not completed.is_set()
                assert not pending.done()
            finally:
                release.set()
                inspection_result = await pending
            assert not inspection_result.isError
    asyncio.run(scenario())
    assert len(upstream["calls"]) == 1
    assert completed.is_set()


@pytest.mark.parametrize("tool,arguments", [
    ("list_runs", {"database": "missing.db"}),
    ("show_run", {"database": "missing.db", "root_id": "missing"}),
    ("run_report", {"database": "missing.db", "root_id": "missing"}),
    ("verify_store", {"database": "missing.db"}),
])
def test_cli_failure_is_an_mcp_error(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, str]
) -> None:
    monkeypatch.setattr(server.subprocess, "run", lambda *_a, **_kw: subprocess.CompletedProcess(
        [], 2, stdout="", stderr="missing recording"
    ))

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            result = await session.call_tool(tool, arguments)
            assert result.isError
            assert "missing recording" in result.content[0].text
    asyncio.run(scenario())


def test_inspection_uses_current_interpreter_and_literal_arguments(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def launch(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=" Japanese: 日本語 \n", stderr="")

    monkeypatch.setattr(server.subprocess, "run", launch)
    output = asyncio.run(server.show_run("-database.db", "--html=unwanted.html"))
    assert output == "Japanese: 日本語"
    argv, options = calls[0]
    assert argv == [
        sys.executable, "-m", "pollard.cli", "show", "--json", "--",
        "-database.db", "--html=unwanted.html"
    ]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["encoding"] == "utf-8"
    assert options["env"]["PYTHONIOENCODING"] == "utf-8"
    assert options["env"]["PATH"] == os.environ["PATH"]


def test_inspection_timeout_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(server.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out after 120 seconds"):
        asyncio.run(server.list_runs())


def test_inspection_round_trip_with_unicode_database(tmp_path: Path) -> None:
    database = tmp_path / "記録.db"
    with SQLiteStore(database) as store:
        run = Runtime(store).run("日本語")
        run.model_call({"prompt": "café"}, fn=lambda _: {
            "text": "done", "usage": {"input_tokens": 1, "output_tokens": 1}
        })
        root = run.root_id
    listed = json.loads(asyncio.run(server.list_runs(str(database))))["runs"]
    assert len(listed) == 1
    assert listed[0]["root_id"] == root
    assert listed[0]["label"] == "日本語"
    tree = json.loads(asyncio.run(server.show_run(str(database), root)))
    assert tree["root_id"] == root
    assert tree["nodes"][0]["label"] == "日本語"
    report = json.loads(asyncio.run(server.run_report(str(database), root)))
    assert report["root_id"] == root
    assert report["spent"]["steps"] == 1
    verification = json.loads(asyncio.run(server.verify_store(str(database))))
    assert verification["ok"]
    assert verification["roots"] == [root]


def test_self_test_validates_real_registration(capsys: pytest.CaptureFixture[str]) -> None:
    server.main(["--self-test"])
    assert set(json.loads(capsys.readouterr().out)["tools"]) == TOOLS


def test_self_test_rejects_missing_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_tools() -> list[Any]:
        return []
    monkeypatch.setattr(server.mcp, "list_tools", no_tools)
    with pytest.raises(RuntimeError, match="unexpected MCP tool registration"):
        server.main(["--self-test"])


def test_help_exits_without_starting_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_start(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("help must not start stdio")
    monkeypatch.setattr(server.mcp, "run", no_start)
    with pytest.raises(SystemExit) as result:
        server.main(["--help"])
    assert result.value.code == 0
    assert "--self-test" in capsys.readouterr().out
