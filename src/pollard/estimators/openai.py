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
            encoding = tiktoken.get_encoding("cl100k_base")
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
