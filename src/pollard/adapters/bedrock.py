"""Adapter for Amazon Bedrock Runtime's Converse API."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from ._common import as_dict, int_field, merge_request


def make_converse_fn(
    client: Any,
    *,
    stream: bool = False,
    count_tokens: bool = False,
    **defaults: Any,
) -> BedrockConverseFn:
    """Return a Pollard step function around a user-owned Bedrock client.

    ``count_tokens=True`` opts into a separate Bedrock ``CountTokens`` request
    during Pollard's precheck. The client and its AWS credentials always remain
    owned by the caller.
    """

    return BedrockConverseFn(
        client=client,
        stream=stream,
        count_tokens=count_tokens,
        defaults=defaults,
    )


@dataclass
class BedrockConverseFn:
    client: Any
    stream: bool = False
    count_tokens: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
        params = merge_request(self.defaults, payload)
        if self.stream:
            response = self.client.converse_stream(**params)
            raw = as_dict(response)
            stream = raw.get("stream")
            if stream is None:
                raise TypeError("Bedrock ConverseStream response is missing stream")
            return _converse_stream(stream)
        return normalize_converse(self.client.converse(**params))

    def estimate_input_tokens(self, payload: dict[str, Any]) -> int | None:
        if not self.count_tokens:
            return None
        params = merge_request(self.defaults, payload)
        model_id = params.pop("modelId", None)
        if not isinstance(model_id, str):
            raise ValueError("Bedrock CountTokens requires a string modelId")
        allowed = {
            key: params[key]
            for key in (
                "messages",
                "system",
                "toolConfig",
                "additionalModelRequestFields",
            )
            if key in params
        }
        response = as_dict(
            self.client.count_tokens(modelId=model_id, input={"converse": allowed})
        )
        value = response.get("inputTokens")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise TypeError("Bedrock CountTokens response is missing inputTokens")


def normalize_converse(response: Any) -> dict[str, Any]:
    """Normalize one Bedrock Converse response into Pollard's result shape."""

    result = as_dict(response)
    result["usage"] = bedrock_usage(result.get("usage"))
    message = _output_message(result)
    text = _message_text(message)
    if text:
        result["text"] = text
    tool_calls = _message_tool_calls(message)
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


def bedrock_usage(value: Any) -> dict[str, int]:
    return {
        "input_tokens": int_field(value, "inputTokens", "input_tokens"),
        "output_tokens": int_field(value, "outputTokens", "output_tokens"),
    }


def _output_message(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, Mapping):
        return {}
    message = output.get("message")
    return dict(message) if isinstance(message, Mapping) else {}


def _message_text(message: Mapping[str, Any]) -> str:
    parts: list[str] = []
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "".join(parts)


def _message_tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    content = message.get("content")
    if not isinstance(content, list):
        return calls
    for block in content:
        if not isinstance(block, Mapping):
            continue
        tool = block.get("toolUse")
        if not isinstance(tool, Mapping):
            continue
        calls.append(
            {
                key: tool[key]
                for key in ("toolUseId", "name", "input")
                if key in tool
            }
        )
    return calls


@dataclass
class _BedrockStreamState:
    role: str | None = None
    text: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    stop_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _converse_stream(stream: Any) -> Iterator[dict[str, Any]]:
    state = _BedrockStreamState()
    for event in stream:
        yield _stream_event(as_dict(event), state)
    yield {"result": _stream_result(state)}


def _stream_event(raw: dict[str, Any], state: _BedrockStreamState) -> dict[str, Any]:
    for name, value in raw.items():
        if name.endswith("Exception"):
            message = value.get("message") if isinstance(value, Mapping) else value
            raise RuntimeError(f"Bedrock stream {name}: {message}")
    chunk: dict[str, Any] = {"event": raw}
    message_start = raw.get("messageStart")
    if isinstance(message_start, Mapping) and isinstance(message_start.get("role"), str):
        state.role = str(message_start["role"])

    block_start = raw.get("contentBlockStart")
    if isinstance(block_start, Mapping):
        _start_tool(block_start, state)

    block_delta = raw.get("contentBlockDelta")
    if isinstance(block_delta, Mapping):
        _apply_delta(block_delta, state, chunk)

    message_stop = raw.get("messageStop")
    if isinstance(message_stop, Mapping) and isinstance(message_stop.get("stopReason"), str):
        state.stop_reason = str(message_stop["stopReason"])

    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        usage = metadata.get("usage")
        if isinstance(usage, Mapping):
            state.usage = bedrock_usage(usage)
        metrics = metadata.get("metrics")
        if isinstance(metrics, Mapping):
            state.metrics = dict(metrics)
    return chunk


def _start_tool(event: Mapping[str, Any], state: _BedrockStreamState) -> None:
    index = event.get("contentBlockIndex", 0)
    start = event.get("start")
    if not isinstance(index, int) or not isinstance(start, Mapping):
        return
    tool = start.get("toolUse")
    if not isinstance(tool, Mapping):
        return
    state.tool_calls[index] = {
        "toolUseId": tool.get("toolUseId", ""),
        "name": tool.get("name", ""),
        "input": "",
    }


def _apply_delta(
    event: Mapping[str, Any],
    state: _BedrockStreamState,
    chunk: dict[str, Any],
) -> None:
    index = event.get("contentBlockIndex", 0)
    delta = event.get("delta")
    if not isinstance(index, int) or not isinstance(delta, Mapping):
        return
    text = delta.get("text")
    if isinstance(text, str):
        state.text.append(text)
        chunk["delta"] = {"text": text}
    tool = delta.get("toolUse")
    if isinstance(tool, Mapping) and isinstance(tool.get("input"), str):
        fragment = str(tool["input"])
        call = state.tool_calls.setdefault(
            index,
            {"toolUseId": "", "name": "", "input": ""},
        )
        call["input"] = str(call.get("input", "")) + fragment
        chunk["delta"] = {"tool_call": {"index": index, "input": fragment}}


def _stream_result(state: _BedrockStreamState) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": "".join(state.text),
        "usage": state.usage,
    }
    if state.role is not None:
        result["role"] = state.role
    if state.stop_reason is not None:
        result["stopReason"] = state.stop_reason
    if state.metrics:
        result["metrics"] = state.metrics
    calls = [_finish_tool(state.tool_calls[index]) for index in sorted(state.tool_calls)]
    if calls:
        result["tool_calls"] = calls
    return result


def _finish_tool(call: dict[str, Any]) -> dict[str, Any]:
    finished = dict(call)
    raw_input = finished.get("input")
    if isinstance(raw_input, str):
        with suppress(json.JSONDecodeError):
            finished["input"] = json.loads(raw_input)
    return finished
