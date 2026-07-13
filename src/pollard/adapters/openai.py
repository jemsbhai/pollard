"""Adapters for clients from the optional ``openai`` dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from ._common import as_dict, int_field, merge_request


def make_responses_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return a Pollard step function for ``client.responses.create``."""

    def call(payload: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
        response = client.responses.create(**params)
        if stream:
            return _responses_stream(response)
        return _normalize_response(response)

    return call


def make_async_responses_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return an async Pollard step function for ``client.responses.create``."""

    async def call(payload: dict[str, Any]) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
        response = await client.responses.create(**params)
        if stream:
            return _async_responses_stream(response)
        return _normalize_response(response)

    return call


def make_chat_completions_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return a Pollard step function for ``client.chat.completions.create``."""

    def call(payload: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
            params.setdefault("stream_options", {"include_usage": True})
        response = client.chat.completions.create(**params)
        if stream:
            return _chat_stream(response)
        return normalize_chat_completion(response)

    return call


def make_async_chat_completions_fn(
    client: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return an async step function for ``client.chat.completions.create``."""

    async def call(payload: dict[str, Any]) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
            params.setdefault("stream_options", {"include_usage": True})
        response = await client.chat.completions.create(**params)
        if stream:
            return _async_chat_stream(response)
        return normalize_chat_completion(response)

    return call


def normalize_chat_completion(response: Any) -> dict[str, Any]:
    """Normalize one OpenAI-compatible chat completion."""

    result = as_dict(response)
    result["usage"] = openai_usage(result)
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    result["text"] = content
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    result["tool_calls"] = tool_calls
    return result


def openai_usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    return {
        "input_tokens": int_field(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": int_field(usage, "output_tokens", "completion_tokens"),
    }


def _normalize_response(response: Any) -> dict[str, Any]:
    result = as_dict(response)
    result["usage"] = openai_usage(result)
    output_text = getattr(response, "output_text", None)
    if not isinstance(output_text, str):
        output_text = result.get("output_text")
    if not isinstance(output_text, str):
        output_text = _responses_text(result)
    if output_text:
        result["text"] = output_text
    tool_calls = _responses_tool_calls(result)
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


def _responses_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _responses_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    output = response.get("output")
    if not isinstance(output, list):
        return calls
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call":
            calls.append(
                {
                    key: item[key]
                    for key in ("call_id", "name", "arguments")
                    if key in item
                }
            )
    return calls


def _responses_stream(stream: Any) -> Iterator[dict[str, Any]]:
    completed = False
    text_parts: list[str] = []
    for event in stream:
        chunk, is_complete = _responses_event(as_dict(event), text_parts)
        completed = completed or is_complete
        yield chunk
    if not completed:
        yield {"result": {"text": "".join(text_parts), "usage": _empty_usage()}}


async def _async_responses_stream(stream: Any) -> AsyncIterator[dict[str, Any]]:
    completed = False
    text_parts: list[str] = []
    async for event in stream:
        chunk, is_complete = _responses_event(as_dict(event), text_parts)
        completed = completed or is_complete
        yield chunk
    if not completed:
        yield {"result": {"text": "".join(text_parts), "usage": _empty_usage()}}


def _responses_event(
    raw: dict[str, Any],
    text_parts: list[str],
) -> tuple[dict[str, Any], bool]:
    event_type = raw.get("type")
    chunk: dict[str, Any] = {"event": raw}
    if event_type == "response.output_text.delta":
        delta = raw.get("delta")
        if isinstance(delta, str):
            text_parts.append(delta)
            chunk["delta"] = {"text": delta}
    if event_type == "response.completed":
        response = raw.get("response")
        if response is not None:
            chunk["result"] = _normalize_response(response)
        else:
            chunk["result"] = {"text": "".join(text_parts), "usage": _empty_usage()}
        return chunk, True
    return chunk, False


@dataclass
class _ChatStreamState:
    text: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    response_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None


def _chat_stream(stream: Any) -> Iterator[dict[str, Any]]:
    state = _ChatStreamState()
    for item in stream:
        yield _chat_chunk(as_dict(item), state)
    yield {"result": _chat_final(state)}


async def _async_chat_stream(stream: Any) -> AsyncIterator[dict[str, Any]]:
    state = _ChatStreamState()
    async for item in stream:
        yield _chat_chunk(as_dict(item), state)
    yield {"result": _chat_final(state)}


def _chat_chunk(raw: dict[str, Any], state: _ChatStreamState) -> dict[str, Any]:
    response_id = raw.get("id")
    model = raw.get("model")
    if isinstance(response_id, str):
        state.response_id = response_id
    if isinstance(model, str):
        state.model = model
    if isinstance(raw.get("usage"), dict):
        state.usage = openai_usage(raw)
    chunk: dict[str, Any] = {"event": raw}
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return chunk
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str):
        state.finish_reason = finish_reason
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return chunk
    content = delta.get("content")
    if isinstance(content, str):
        state.text.append(content)
        chunk["delta"] = {"text": content}
    calls = delta.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict):
                _merge_chat_tool_call(state, call)
    return chunk


def _merge_chat_tool_call(state: _ChatStreamState, fragment: dict[str, Any]) -> None:
    index = fragment.get("index", 0)
    if not isinstance(index, int):
        index = 0
    call = state.tool_calls.setdefault(index, {"function": {"name": "", "arguments": ""}})
    for key in ("id", "type"):
        value = fragment.get(key)
        if isinstance(value, str):
            call[key] = value
    function = fragment.get("function")
    target = call["function"]
    if isinstance(function, dict) and isinstance(target, dict):
        for key in ("name", "arguments"):
            value = function.get(key)
            if isinstance(value, str):
                target[key] = str(target.get(key, "")) + value


def _chat_final(state: _ChatStreamState) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": "".join(state.text),
        "usage": state.usage,
    }
    if state.response_id is not None:
        result["id"] = state.response_id
    if state.model is not None:
        result["model"] = state.model
    if state.finish_reason is not None:
        result["finish_reason"] = state.finish_reason
    if state.tool_calls:
        result["tool_calls"] = [state.tool_calls[index] for index in sorted(state.tool_calls)]
    return result


def _empty_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0}
