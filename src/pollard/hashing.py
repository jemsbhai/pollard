"""Hash helpers for pollard node identity and stored results."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ._canon import IdentityValue, canonical_bytes

DOMAIN = b"pollard/v1\n"
RESULT_DOMAIN = b"pollard/v1:result\n"


def node_id(kind: str, parent_id: str | None, attempt: int, payload: IdentityValue) -> str:
    """Compute a pollard node id."""

    identity: dict[str, IdentityValue] = {
        "a": attempt,
        "k": kind,
        "p": parent_id or "",
        "pl": payload,
    }
    return hashlib.sha256(DOMAIN + canonical_bytes(identity)).hexdigest()


def digest_payload(payload: IdentityValue) -> str:
    """Digest an identity payload for compact audit references."""

    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def result_to_text(result: Any) -> str:
    """Serialize a stored result to stable JSON text."""

    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def result_digest_from_text(result_text: str) -> str:
    """Return the integrity digest for already serialized result text."""

    return hashlib.sha256(RESULT_DOMAIN + result_text.encode("utf-8")).hexdigest()


def result_text_and_digest(result: Any) -> tuple[str, str]:
    """Serialize a result once and return its digest."""

    text = result_to_text(result)
    return text, result_digest_from_text(text)
