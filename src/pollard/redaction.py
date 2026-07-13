"""Redaction markers for identity payloads."""

from __future__ import annotations

import hashlib

from ._canon import IdentityValue, canonical_bytes

REDACTION_DOMAIN = b"pollard/v1:redact\n"
REDACTION_KEY = "__pollard_redacted"


def redact(value: IdentityValue, hint: str | None = None) -> dict[str, IdentityValue]:
    """Replace an identity value with a deterministic, content-committing marker."""

    digest = hashlib.sha256(REDACTION_DOMAIN + canonical_bytes(value)).hexdigest()
    return {REDACTION_KEY: digest, "hint": hint}


def is_redacted(value: object) -> bool:
    """Return whether a value has Pollard's exact redaction-marker shape."""

    if not isinstance(value, dict) or set(value) != {REDACTION_KEY, "hint"}:
        return False
    digest = value.get(REDACTION_KEY)
    hint = value.get("hint")
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and (hint is None or isinstance(hint, str))
    )


def contains_redaction(value: object) -> bool:
    """Return whether a nested value contains a Pollard redaction marker."""

    if is_redacted(value):
        return True
    if isinstance(value, dict):
        return any(contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_redaction(item) for item in value)
    return False
