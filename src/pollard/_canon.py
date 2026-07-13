"""Canonical serialization for identity payloads."""

from __future__ import annotations

import json
from typing import Any, TypeAlias

IdentityValue: TypeAlias = (
    dict[str, "IdentityValue"] | list["IdentityValue"] | str | int | bool | None
)


def canonical_bytes(obj: IdentityValue) -> bytes:
    """Return stable UTF-8 JSON bytes for an identity value.

    Floats and non-JSON identity types are rejected because these bytes are a
    compatibility surface for node identity.
    """

    _validate_identity_value(obj, "$")
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _validate_identity_value(obj: Any, path: str) -> None:
    if obj is None or isinstance(obj, (str, bool)):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        raise TypeError(f"floats are not allowed in identity payloads at {path}")
    if isinstance(obj, bytes):
        raise TypeError(f"bytes are not allowed in identity payloads at {path}")
    if isinstance(obj, list):
        for index, item in enumerate(obj):
            _validate_identity_value(item, f"{path}[{index}]")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"identity object keys must be str at {path}")
            _validate_identity_value(value, f"{path}.{key}")
        return
    raise TypeError(f"unsupported identity value {type(obj).__name__} at {path}")
