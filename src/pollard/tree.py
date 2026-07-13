"""Node records for the content-addressed execution tree."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._canon import IdentityValue, canonical_bytes
from .hashing import node_id, result_digest_from_text, result_text_and_digest

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class NodeKind(str, Enum):
    ROOT = "root"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    NOTE = "note"
    REFUSAL = "refusal"


@dataclass(frozen=True)
class Node:
    id: str
    parent: str | None
    kind: str
    attempt: int
    payload: dict[str, IdentityValue]
    result: Any = None
    result_digest: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    _result_text: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def make(
        cls,
        *,
        kind: str | NodeKind,
        parent: str | None,
        payload: dict[str, IdentityValue],
        attempt: int = 0,
        result: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> Node:
        kind_value = kind.value if isinstance(kind, NodeKind) else kind
        text: str | None = None
        digest: str | None = None
        if result is not None:
            text, digest = result_text_and_digest(result)
        return cls(
            id=node_id(kind_value, parent, attempt, payload),
            parent=parent,
            kind=kind_value,
            attempt=attempt,
            payload=payload,
            result=result,
            result_digest=digest,
            meta={} if meta is None else dict(meta),
            _result_text=text,
        )

    @classmethod
    def from_storage(
        cls,
        *,
        id: str,
        parent: str | None,
        kind: str,
        attempt: int,
        payload_text: str,
        result_text: str | None,
        result_digest: str | None,
        meta_text: str,
    ) -> Node:
        payload = json.loads(payload_text)
        result = None if result_text is None else json.loads(result_text)
        meta = json.loads(meta_text)
        if not isinstance(payload, dict):
            raise TypeError("stored payload must be a JSON object")
        if not isinstance(meta, dict):
            raise TypeError("stored meta must be a JSON object")
        return cls(
            id=id,
            parent=parent,
            kind=kind,
            attempt=attempt,
            payload=payload,
            result=result,
            result_digest=result_digest,
            meta=meta,
            _result_text=result_text,
        )

    def __post_init__(self) -> None:
        if self.kind not in {kind.value for kind in NodeKind}:
            raise ValueError(f"unsupported node kind: {self.kind}")
        if not _is_hex64(self.id):
            raise ValueError("node id must be 64 lowercase hex characters")
        if self.kind == NodeKind.ROOT.value:
            if self.parent is not None:
                raise ValueError("root nodes cannot have parents")
        elif not _is_hex64(self.parent):
            raise ValueError("non-root nodes require a 64-hex parent id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ValueError("attempt must be a non-negative int")
        canonical_bytes(self.payload)
        _validate_json(self.meta, "meta")
        if self.result is not None and self._result_text is None:
            text, digest = result_text_and_digest(self.result)
            object.__setattr__(self, "_result_text", text)
            if self.result_digest is None:
                object.__setattr__(self, "result_digest", digest)
        if self._result_text is not None:
            json.loads(self._result_text)
            if self.result_digest is None:
                object.__setattr__(
                    self,
                    "result_digest",
                    result_digest_from_text(self._result_text),
                )
        if self.result_digest is not None and not _is_hex64(self.result_digest):
            raise ValueError("result digest must be 64 lowercase hex characters")

    @property
    def expected_id(self) -> str:
        return node_id(self.kind, self.parent, self.attempt, self.payload)

    @property
    def result_text(self) -> str | None:
        return self._result_text

    def identity_tuple(self) -> tuple[str | None, str, int, dict[str, IdentityValue]]:
        return self.parent, self.kind, self.attempt, self.payload


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.match(value) is not None


def _validate_json(value: Any, label: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON serializable") from exc
