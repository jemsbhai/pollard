"""Adapter for the optional ``litellm`` dependency."""

from __future__ import annotations

from typing import Any

from ._common import merge_request
from .openai import _async_chat_stream, _chat_stream, normalize_chat_completion


def make_completion_fn(
    completion: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return a Pollard step function around ``litellm.completion``."""

    def call(payload: dict[str, Any]) -> Any:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
            params.setdefault("stream_options", {"include_usage": True})
        response = completion(**params)
        if stream:
            return _chat_stream(response)
        return normalize_chat_completion(response)

    return call


def make_async_completion_fn(
    acompletion: Any,
    *,
    stream: bool = False,
    **defaults: Any,
) -> Any:
    """Return an async Pollard step function around ``litellm.acompletion``."""

    async def call(payload: dict[str, Any]) -> Any:
        params = merge_request(defaults, payload)
        if stream:
            params["stream"] = True
            params.setdefault("stream_options", {"include_usage": True})
        response = await acompletion(**params)
        if stream:
            return _async_chat_stream(response)
        return normalize_chat_completion(response)

    return call
