"""Shared duck-typed helpers for provider adapters."""

from __future__ import annotations

import copy
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
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                return item
    return 0


def has_nonnegative_int_field(value: Any, *names: str) -> bool:
    return isinstance(value, Mapping) and any(
        _is_nonnegative_int(value.get(name)) for name in names
    )


def set_normalized_usage(
    result: dict[str, Any],
    normalized: dict[str, int],
    *,
    required_fields: tuple[tuple[str, ...], ...],
    optional_fields: tuple[str, ...] = (),
) -> bool:
    """Preserve provider usage and install Pollard's normalized usage shape."""

    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        result.pop("usage", None)
        return False
    result["provider_usage"] = copy.deepcopy(dict(usage))
    valid_required = all(
        any(_is_nonnegative_int(usage.get(name)) for name in alternatives)
        for alternatives in required_fields
    )
    valid_optional = all(
        name not in usage or _is_nonnegative_int(usage.get(name))
        for name in optional_fields
    )
    if not valid_required or not valid_optional:
        result.pop("usage", None)
        return False
    result["usage"] = normalized
    return True


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def merge_request(defaults: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    request = {**defaults, **payload}
    request.pop("_pollard", None)
    return request


@contextmanager
def post_dispatch_boundary() -> Iterator[None]:
    """Preserve a native error while marking its dispatch outcome unknown."""

    try:
        yield
    except BaseException as error:
        marked = mark_post_dispatch_outcome_unknown(error)
        if marked is error:
            raise
        raise marked from error
