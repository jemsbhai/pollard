"""Shared duck-typed helpers for provider adapters."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from ..errors import mark_post_dispatch_outcome_unknown


def as_dict(value: Any) -> dict[str, Any]:
    """Convert an SDK model or mapping into a plain dictionary."""

    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            converted = method()
            if isinstance(converted, Mapping):
                return dict(converted)
    raise TypeError("provider response must be a mapping or expose to_dict/model_dump")


def int_field(value: Any, *names: str) -> int:
    if isinstance(value, Mapping):
        for name in names:
            item = value.get(name)
            if isinstance(item, int) and not isinstance(item, bool):
                return item
    return 0


def merge_request(defaults: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    request = {**defaults, **payload}
    request.pop("_pollard", None)
    return request


@contextmanager
def post_dispatch_boundary() -> Iterator[None]:
    """Preserve a native error while marking its dispatch outcome unknown."""

    try:
        yield
    except Exception as error:
        marked = mark_post_dispatch_outcome_unknown(error)
        if marked is error:
            raise
        raise marked from error
