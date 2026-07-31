"""A tiktoken-backed approximation for OpenAI-style payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class OpenAITokenEstimator:
    """Estimate input tokens by encoding textual leaves plus message overhead.

    This is an approximation, not a provider bill prediction. Images, tools,
    provider-added instructions, and future wire-format changes can add tokens.
    """

    def __init__(self, model: str | None = None, *, tokens_per_message: int = 3) -> None:
        if isinstance(tokens_per_message, bool) or tokens_per_message < 0:
            raise ValueError("tokens_per_message must be a non-negative int")
        self._model = model
        self._tokens_per_message = tokens_per_message

    def estimate_input_tokens(self, payload: dict[str, Any]) -> int | None:
        try:
            import tiktoken
        except ImportError as exc:
            raise ImportError(
                "OpenAITokenEstimator requires pollard[estimate-openai]"
            ) from exc
        model = self._model or payload.get("model")
        try:
            encoding = tiktoken.encoding_for_model(model) if isinstance(model, str) else None
        except KeyError:
            encoding = None
        if encoding is None:
            encoding = tiktoken.get_encoding(_fallback_encoding_name(model))
        total = _count_textual_leaves(payload, encoding)
        messages = payload.get("messages")
        if isinstance(messages, list):
            total += len(messages) * self._tokens_per_message
        return total


def _count_textual_leaves(value: Any, encoding: Any, *, key: str | None = None) -> int:
    if isinstance(value, str):
        return 0 if key == "model" else len(encoding.encode(value))
    if isinstance(value, Mapping):
        return sum(
            _count_textual_leaves(item, encoding, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return sum(_count_textual_leaves(item, encoding) for item in value)
    return 0


def _fallback_encoding_name(model: Any) -> str:
    """Choose a family-safe fallback when an older tiktoken lacks a model id."""

    if not isinstance(model, str):
        return "cl100k_base"
    model_name = model.rsplit(":", 1)[-1].lower()
    modern_prefixes = (
        "gpt-5",
        "gpt-4.5",
        "gpt-4.1",
        "gpt-4o",
        "chatgpt-4o",
        "o1",
        "o3",
        "o4-mini",
    )
    return "o200k_base" if model_name.startswith(modern_prefixes) else "cl100k_base"
