"""Adapters for clients from the optional ``anthropic`` dependency."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from ._common import as_dict, int_field, merge_request, post_dispatch_boundary

_COUNT_TOKEN_FIELDS = (
    "model",
    "messages",
    "cache_control",
    "output_config",
    "output_format",
    "system",
    "thinking",
    "tool_choice",
    "tools",
    "extra_body",
    "extra_headers",
    "extra_query",
    "timeout",
)


def make_messages_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> AnthropicMessagesFn:
    """Return a callable that is also a network-backed token estimator."""

    return AnthropicMessagesFn(client=client, stream=stream, defaults=defaults)


def make_async_messages_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return an async Pollard step function for ``client.messages.create``."""

    async def call(payload: dict[str, Any]) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
        with post_dispatch_boundary():
            response = await client.messages.create(**params)
            if not stream:
                return normalize_message(response)
        if stream:
            return _async_messages_stream(response)
        raise AssertionError("unreachable")

    return call


@dataclass(frozen=True)
class AnthropicMessagesFn:
    """Sync Messages API function with ``estimate_input_tokens`` support.

    The estimator calls ``client.messages.count_tokens`` and therefore performs
    a provider request during Pollard's precheck. Callers opt into that request
    by passing this object to ``TokenMeter(estimator=...)``.
    """

    client: Any
    stream: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
        params = merge_request(self.defaults, payload)
        if self.stream:
            params["stream"] = True
        with post_dispatch_boundary():
            response = self.client.messages.create(**params)
            if not self.stream:
                return normalize_message(response)
        if self.stream:
            return _messages_stream(response)
        raise AssertionError("unreachable")

    def estimate_input_tokens(self, payload: dict[str, Any]) -> int | None:
        request = merge_request(self.defaults, payload)
        params = {key: request[key] for key in _COUNT_TOKEN_FIELDS if key in request}
        counted = self.client.messages.count_tokens(**params)
        if isinstance(counted, int) and not isinstance(counted, bool):
            return counted
        raw = as_dict(counted)
        value = raw.get("input_tokens")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


def normalize_message(response: Any) -> dict[str, Any]:
    """Normalize an Anthropic Message to Pollard's result contract."""

    result = as_dict(response)
    result["usage"] = anthropic_usage(result)
    content = result.get("content")
    if not isinstance(content, list):
        return result
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(
                {
                    key: block[key]
                    for key in ("id", "name", "input")
                    if key in block
                }
            )
    if text:
        result["text"] = "".join(text)
    if calls:
        result["tool_calls"] = calls
    return result


def anthropic_usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    return {
        "input_tokens": int_field(usage, "input_tokens"),
        "output_tokens": int_field(usage, "output_tokens"),
    }


@dataclass
class _AnthropicStreamState:
    text: list[str] = field(default_factory=list)
    tools: dict[int, dict[str, Any]] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    message_id: str | None = None
    model: str | None = None
    stop_reason: str | None = None


def _messages_stream(stream: Any) -> Iterator[dict[str, Any]]:
    state = _AnthropicStreamState()
    with post_dispatch_boundary():
        for event in stream:
            yield _message_event(as_dict(event), state)
    yield {"result": _message_final(state)}


async def _async_messages_stream(stream: Any) -> AsyncIterator[dict[str, Any]]:
    state = _AnthropicStreamState()
    with post_dispatch_boundary():
        async for event in stream:
            yield _message_event(as_dict(event), state)
    yield {"result": _message_final(state)}


def _message_event(raw: dict[str, Any], state: _AnthropicStreamState) -> dict[str, Any]:
    event_type = raw.get("type")
    chunk: dict[str, Any] = {"event": raw}
    if event_type == "message_start":
        message = raw.get("message")
        if isinstance(message, dict):
            message_id = message.get("id")
            model = message.get("model")
            if isinstance(message_id, str):
                state.message_id = message_id
            if isinstance(model, str):
                state.model = model
            state.input_tokens = int_field(message.get("usage"), "input_tokens")
    elif event_type == "content_block_start":
        _start_content_block(raw, state)
    elif event_type == "content_block_delta":
        text = _content_delta(raw, state)
        if text is not None:
            chunk["delta"] = {"text": text}
    elif event_type == "message_delta":
        delta = raw.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
            state.stop_reason = delta["stop_reason"]
        state.output_tokens = int_field(raw.get("usage"), "output_tokens")
    return chunk


def _start_content_block(raw: dict[str, Any], state: _AnthropicStreamState) -> None:
    index = raw.get("index", 0)
    block = raw.get("content_block")
    if not isinstance(index, int) or not isinstance(block, dict):
        return
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        state.text.append(block["text"])
    elif block.get("type") == "tool_use":
        state.tools[index] = {
            "id": block.get("id"),
            "name": block.get("name"),
            "input_json": "",
        }


def _content_delta(raw: dict[str, Any], state: _AnthropicStreamState) -> str | None:
    index = raw.get("index", 0)
    delta = raw.get("delta")
    if not isinstance(index, int) or not isinstance(delta, dict):
        return None
    if delta.get("type") == "text_delta":
        text = delta.get("text")
        if isinstance(text, str):
            state.text.append(text)
            return text
    elif delta.get("type") == "input_json_delta":
        partial = delta.get("partial_json")
        tool = state.tools.get(index)
        if isinstance(partial, str) and tool is not None:
            tool["input_json"] = str(tool.get("input_json", "")) + partial
    return None


def _message_final(state: _AnthropicStreamState) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": "".join(state.text),
        "usage": {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
        },
    }
    if state.message_id is not None:
        result["id"] = state.message_id
    if state.model is not None:
        result["model"] = state.model
    if state.stop_reason is not None:
        result["stop_reason"] = state.stop_reason
    if state.tools:
        result["tool_calls"] = [_finish_tool(state.tools[index]) for index in sorted(state.tools)]
    return result


def _finish_tool(tool: dict[str, Any]) -> dict[str, Any]:
    finished = {key: tool[key] for key in ("id", "name") if isinstance(tool.get(key), str)}
    source = tool.get("input_json", "")
    if isinstance(source, str):
        try:
            parsed = json.loads(source) if source else {}
        except json.JSONDecodeError:
            finished["input_json"] = source
        else:
            finished["input"] = parsed
    return finished
