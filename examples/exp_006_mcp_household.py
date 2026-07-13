"""Record EXP-006C: household planning across three pinned MCP servers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from _exp006_common import (
    finalize_case,
    local_llama_server,
    sha256_file,
    write_json,
)

from pollard import AsyncRuntime, Budget, Registry, SQLiteStore
from pollard.adapters.openai import make_chat_completions_fn
from pollard.mcp import registry_from_mcp

LIMIT_CENTS = 2_000
REJECTED_NAMES = ("laundry detergent", "paper towels", "premium air freshener")
SELECTED_NAMES = ("laundry detergent", "dish soap", "sponges")


def _structured(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("MCP call omitted its result object")
    for key in ("structuredContent", "structured_content", "result"):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                value = json.loads(item["text"])
                if isinstance(value, dict):
                    return value
    if any(key in result for key in ("items", "total_cents", "approved")):
        return result
    raise RuntimeError(f"MCP result has no structured object: {result}")


async def _open_mcp_registry(
    stack: AsyncExitStack,
    server_paths: list[Path],
) -> Registry:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    specs = []
    for server_path in server_paths:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(server_path.resolve())],
        )
        read, write = await stack.enter_async_context(stdio_client(parameters))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        registry = await registry_from_mcp(session)
        specs.extend(registry)
    return Registry(specs)


def _catalog_prices(catalog: dict[str, Any]) -> dict[str, int]:
    items = catalog.get("items")
    if not isinstance(items, list):
        raise RuntimeError("catalog MCP response omitted items")
    prices: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("catalog item is not an object")
        prices[str(item["name"])] = int(item["price_cents"])
    return prices


async def _record(args: argparse.Namespace) -> dict[str, Any]:
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "run.db"
    db_path.unlink(missing_ok=True)
    server_paths = sorted(args.servers.glob("*_server.py"))
    if [path.name for path in server_paths] != [
        "catalog_server.py",
        "math_server.py",
        "policy_server.py",
    ]:
        raise RuntimeError("expected exactly the three pinned EXP-006C MCP servers")

    async with AsyncExitStack() as stack:
        registry = await _open_mcp_registry(stack, server_paths)
        with local_llama_server(args.server_binary, args.model, port=args.port) as client:
            call_model_sync = make_chat_completions_fn(
                client,
                max_tokens=320,
                seed=6006,
                temperature=0,
            )

            async def call_model(payload: dict[str, Any]) -> dict[str, Any]:
                return call_model_sync(payload)

            with SQLiteStore(db_path) as store:
                runtime = AsyncRuntime(store, registry=registry, mode="record")
                async with runtime.run(
                    "exp-006c-household-mcp",
                    budget=Budget(steps=24),
                ) as agent:
                    agent.note(
                        {
                            "case": "EXP-006C",
                            "adapter": "openai-compatible-chat",
                            "mcp_transport": "stdio",
                            "model": args.model_id,
                            "network": "loopback-and-local-stdio-only",
                        }
                    )
                    catalog_node = await agent.atool_call(
                        "lookup_household_items",
                        {"query": "weekly household cleaning essentials"},
                        version="mcp",
                    )
                    catalog = _structured(catalog_node.result)
                    prices = _catalog_prices(catalog)
                    branch_parent = agent.cursor_id

                    async with agent.branch(attempt=0) as rejected:
                        await rejected.amodel_call(
                            {
                                "model": args.model_id,
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "Review the proposed household order against "
                                            "the supplied catalog and budget."
                                        ),
                                    },
                                    {
                                        "role": "user",
                                        "content": json.dumps(
                                            {
                                                "catalog": catalog,
                                                "limit_cents": LIMIT_CENTS,
                                                "proposal": REJECTED_NAMES,
                                            },
                                            sort_keys=True,
                                        ),
                                    },
                                ],
                            },
                            fn=call_model,
                        )
                        rejected_sum_node = await rejected.atool_call(
                            "sum_prices",
                            {"prices_cents": [prices[name] for name in REJECTED_NAMES]},
                            version="mcp",
                        )
                        rejected_sum = _structured(rejected_sum_node.result)
                        rejected_policy_node = await rejected.atool_call(
                            "check_household_budget",
                            {
                                "total_cents": int(rejected_sum["total_cents"]),
                                "limit_cents": LIMIT_CENTS,
                            },
                            version="mcp",
                        )
                        rejected_policy = _structured(rejected_policy_node.result)
                        if rejected_policy.get("approved") is not False:
                            raise RuntimeError("over-budget MCP branch unexpectedly passed")
                        rejected.note({"decision": "reject-over-budget-order"})
                        rejected.prune()
                        rejected_tip = rejected.cursor_id

                    agent.rollback(branch_parent)
                    async with agent.branch(attempt=1) as selected:
                        await selected.amodel_call(
                            {
                                "model": args.model_id,
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "Review the revised household order against "
                                            "the supplied catalog and budget."
                                        ),
                                    },
                                    {
                                        "role": "user",
                                        "content": json.dumps(
                                            {
                                                "catalog": catalog,
                                                "limit_cents": LIMIT_CENTS,
                                                "proposal": SELECTED_NAMES,
                                            },
                                            sort_keys=True,
                                        ),
                                    },
                                ],
                            },
                            fn=call_model,
                        )
                        selected_sum_node = await selected.atool_call(
                            "sum_prices",
                            {"prices_cents": [prices[name] for name in SELECTED_NAMES]},
                            version="mcp",
                        )
                        selected_sum = _structured(selected_sum_node.result)
                        selected_policy_node = await selected.atool_call(
                            "check_household_budget",
                            {
                                "total_cents": int(selected_sum["total_cents"]),
                                "limit_cents": LIMIT_CENTS,
                            },
                            version="mcp",
                        )
                        selected_policy = _structured(selected_policy_node.result)
                        if selected_policy.get("approved") is not True:
                            raise RuntimeError("in-budget MCP branch unexpectedly failed")
                        selected.note({"decision": "select-in-budget-order"})
                        selected_tip = selected.cursor_id
                    root_id = agent.root_id
                    report = agent.report()

    artifact = finalize_case(db_path, root_id, output_dir)
    outcome = {
        "id": "EXP-006C",
        "status": "passed",
        "workload": "household-order-over-three-local-mcp-servers",
        "adapter": "pollard.adapters.openai.make_chat_completions_fn",
        "model": {
            "id": args.model_id,
            "sha256": sha256_file(args.model),
            "llama_cpp_release": args.llama_release,
            "server_sha256": sha256_file(args.server_binary),
        },
        "mcp": {
            "transport": "stdio",
            "servers": {path.name: sha256_file(path) for path in server_paths},
        },
        "registry_digest": registry.registry_digest,
        "limit_cents": LIMIT_CENTS,
        "catalog": catalog,
        "rejected": {
            "items": list(REJECTED_NAMES),
            "sum": rejected_sum,
            "policy": rejected_policy,
            "tip": rejected_tip,
        },
        "selected": {
            "items": list(SELECTED_NAMES),
            "sum": selected_sum,
            "policy": selected_policy,
            "tip": selected_tip,
        },
        "report": report,
        "artifact": artifact,
        "provider_spend_usd": 0,
    }
    write_json(output_dir / "outcome.json", outcome)
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", default="qwen2.5-coder:7b")
    parser.add_argument("--llama-release", default="b9630")
    parser.add_argument(
        "--servers",
        type=Path,
        default=Path("evidence/EXP-006/inputs/mcp/servers"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/EXP-006/mcp-household"),
    )
    parser.add_argument("--port", type=int, default=8133)
    args = parser.parse_args()
    outcome = asyncio.run(_record(args))
    print(outcome["artifact"]["seal_digest"])


if __name__ == "__main__":
    main()
