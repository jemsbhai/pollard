"""Record/replay mode helpers."""

from __future__ import annotations

from enum import Enum
from typing import Any

from ._canon import IdentityValue
from .errors import IntegrityError, MissingRecording
from .governor import charge_to_decimal, charge_to_json
from .hashing import digest_payload
from .meters import Meter
from .store import Store
from .tree import Node, NodeKind
from .verify import verify


class ReplayMode(str, Enum):
    RECORD = "record"
    HYBRID = "hybrid"
    REPLAY = "replay"


def normalize_mode(mode: str | ReplayMode) -> ReplayMode:
    if isinstance(mode, ReplayMode):
        return mode
    try:
        return ReplayMode(mode)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ReplayMode)
        raise ValueError(f"mode must be one of: {allowed}") from exc


def recorded_node_or_missing(
    *,
    mode: ReplayMode,
    store: Store,
    kind: NodeKind,
    parent_id: str,
    payload: dict[str, IdentityValue],
    attempt: int,
) -> Node | None:
    if mode == ReplayMode.RECORD:
        return None
    candidate = Node.make(kind=kind, parent=parent_id, payload=payload, attempt=attempt)
    if not store.exists(candidate.id):
        if mode == ReplayMode.REPLAY:
            _raise_missing(candidate, kind.value, payload)
        return None
    node = store.get(candidate.id)
    if node.result_text is None:
        if mode == ReplayMode.REPLAY:
            _raise_missing(candidate, kind.value, payload)
        return None
    if mode == ReplayMode.REPLAY:
        report = verify(store, node.id)
        if not report.ok:
            details = "; ".join(
                f"{finding.node_id}: {finding.message}" for finding in report.findings
            )
            raise IntegrityError(f"recording integrity check failed: {details}")
    return node


def avoided_charges(
    *,
    meters: list[Meter],
    kind: str,
    payload: dict[str, IdentityValue],
    node: Node,
) -> dict[str, int | float]:
    charges: dict[str, int | float] = {}
    payload_any: dict[str, Any] = payload
    for meter in meters:
        amount = charge_to_decimal(meter.charge(kind, payload_any, node.result, node.meta))
        if amount != 0:
            charges[meter.name] = charge_to_json(amount)
    return charges


def _raise_missing(
    candidate: Node,
    kind: str,
    payload: dict[str, IdentityValue],
) -> None:
    summary = payload_summary(kind, payload)
    raise MissingRecording(
        f"missing recording for {summary}: {candidate.id}",
        candidate.id,
        summary,
    )


def payload_summary(kind: str, payload: dict[str, IdentityValue]) -> str:
    parts = [kind]
    model = payload.get("model")
    if kind == NodeKind.MODEL_CALL.value and isinstance(model, str):
        parts.append(f"model={model}")
    tool = payload.get("tool")
    if kind == NodeKind.TOOL_CALL.value and isinstance(tool, str):
        version = payload.get("version")
        label = tool if not isinstance(version, str) else f"{tool}@{version}"
        parts.append(f"tool={label}")
    parts.append(f"digest={digest_payload(payload)}")
    return " ".join(parts)
