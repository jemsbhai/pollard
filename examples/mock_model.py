"""Deterministic offline helpers for pollard examples."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def call_model(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "text": f"mock response {digest[:12]}",
        "usage": {
            "input_tokens": max(1, len(text) // 4),
            "output_tokens": 12,
        },
    }


def judge(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args", {})
    text = args.get("text", "") if isinstance(args, dict) else ""
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    return {
        "value": str(int(digest[:4], 16) % 100),
        "usage": {
            "input_tokens": max(1, len(str(text)) // 4),
            "output_tokens": 1,
        },
    }
