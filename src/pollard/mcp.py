"""MCP tool-list adapter."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from .errors import UnsupportedSchema
from .registry import ActionSpec, Registry


async def registry_from_mcp(session: Any, *, exclude: set[str] | None = None) -> Registry:
    excluded = exclude or set()
    listing = await _maybe_await(session.list_tools())
    tools = _tool_list(listing)
    specs: list[ActionSpec] = []
    for tool in tools:
        name = _tool_field(tool, "name")
        if name in excluded:
            continue
        schema = _tool_schema(tool)
        try:
            spec = ActionSpec(
                name=name,
                version="mcp",
                description=_tool_field(tool, "description", default=""),
                schema=schema,
                side_effects=True,
                handler=_make_handler(session, name),
            )
        except UnsupportedSchema as exc:
            raise UnsupportedSchema(f"MCP tool {name}: {exc}") from exc
        specs.append(spec)
    return Registry(specs)


def _make_handler(session: Any, name: str) -> Any:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await _maybe_await(session.call_tool(name, args))
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            dumped = result.model_dump()
            normalized = _to_jsonable(dumped)
            return normalized if isinstance(normalized, dict) else {"result": normalized}
        if hasattr(result, "content"):
            return {"content": _to_jsonable(result.content)}
        return {"result": _to_jsonable(result)}

    return handler


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


def _tool_list(listing: Any) -> list[Any]:
    tools = listing.get("tools", []) if isinstance(listing, dict) else getattr(listing, "tools", [])
    if not isinstance(tools, list):
        raise TypeError("MCP tools/list response must contain a tools list")
    return tools


def _tool_schema(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema", tool.get("input_schema", {}))
    else:
        schema = getattr(tool, "inputSchema", getattr(tool, "input_schema", {}))
    if not isinstance(schema, dict):
        raise TypeError("MCP tool schema must be an object")
    return schema


def _tool_field(tool: Any, field: str, *, default: str | None = None) -> str:
    value = tool.get(field, default) if isinstance(tool, dict) else getattr(tool, field, default)
    if isinstance(value, str):
        return value
    if default is not None:
        return default
    raise TypeError(f"MCP tool missing string field {field}")


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | bool | float):
        return value
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    return str(value)
