"""Conflict-aware union of append-only Pollard stores."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import IntegrityError
from .store import Store
from .tree import Node


@dataclass(frozen=True)
class MergeReport:
    """Summary of a store merge."""

    copied: int
    existing: int
    result_conflicts: int
    meta_conflicts: int

    def to_dict(self) -> dict[str, int]:
        return {
            "copied": self.copied,
            "existing": self.existing,
            "result_conflicts": self.result_conflicts,
            "meta_conflicts": self.meta_conflicts,
        }


def merge(dst: Store, src: Store, *, replay: bool = False) -> MergeReport:
    """Union every node in ``src`` into ``dst`` without discarding metadata.

    Identity collisions are integrity failures. A result collision keeps the
    destination result and records the incoming result, unless ``replay`` is
    true, where nondeterministic results are rejected.
    """

    copied = 0
    existing_count = 0
    result_conflict_count = 0
    meta_conflict_count = 0
    seen: set[str] = set()
    incoming_nodes: list[Node] = []
    for root_id in src.roots():
        for incoming in src.walk(root_id):
            if incoming.id in seen:
                continue
            seen.add(incoming.id)
            incoming_nodes.append(incoming)
    for incoming in incoming_nodes:
        if not dst.exists(incoming.id):
            continue
        existing = dst.get(incoming.id)
        if existing.identity_tuple() != incoming.identity_tuple():
            raise IntegrityError(f"node id collision for {incoming.id}")
        if (
            replay
            and incoming.result_text is not None
            and incoming.result_text != existing.result_text
        ):
            raise IntegrityError(f"result collision during replay merge: {incoming.id}")

    for incoming in incoming_nodes:
        if not dst.exists(incoming.id):
            dst.put(incoming)
            copied += 1
            continue

        existing = dst.get(incoming.id)
        existing_count += 1
        result_conflict = (
            incoming.result_text is not None
            and incoming.result_text != existing.result_text
        )
        merged_meta, new_meta_conflicts = _merge_meta(existing.meta, incoming.meta)
        meta_conflict_count += new_meta_conflicts
        if result_conflict:
            conflict = {
                "result_digest": incoming.result_digest,
                "result": incoming.result,
            }
            result_conflicts = _union_json_lists(
                _list_value(merged_meta.get("result_conflicts")),
                [conflict],
            )
            if len(result_conflicts) > len(
                _list_value(merged_meta.get("result_conflicts"))
            ):
                result_conflict_count += 1
            merged_meta["result_conflicts"] = result_conflicts
        if merged_meta != existing.meta:
            dst.update_meta(existing.id, merged_meta)

    return MergeReport(
        copied=copied,
        existing=existing_count,
        result_conflicts=result_conflict_count,
        meta_conflicts=meta_conflict_count,
    )


def _merge_meta(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    merged = dict(existing)
    recorded = _union_json_lists(
        _list_value(existing.get("merge_conflicts")),
        _list_value(incoming.get("merge_conflicts")),
    )
    conflicts: list[dict[str, Any]] = []
    for key in sorted(set(incoming) - {"merge_conflicts"}):
        if key not in merged:
            merged[key] = incoming[key]
            continue
        value, found = _merge_meta_value(merged[key], incoming[key], (key,))
        merged[key] = value
        conflicts.extend(found)
    updated = _union_json_lists(recorded, conflicts)
    if updated:
        merged["merge_conflicts"] = updated
    return merged, len(updated) - len(recorded)


def _merge_meta_value(
    existing: Any,
    incoming: Any,
    path: tuple[str, ...],
) -> tuple[Any, list[dict[str, Any]]]:
    if existing == incoming:
        return existing, []
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        conflicts: list[dict[str, Any]] = []
        for key in sorted(incoming):
            if key not in merged:
                merged[key] = incoming[key]
                continue
            merged[key], nested = _merge_meta_value(
                merged[key], incoming[key], (*path, str(key))
            )
            conflicts.extend(nested)
        return merged, conflicts
    if isinstance(existing, list) and isinstance(incoming, list):
        return _union_json_lists(existing, incoming), []
    return existing, [
        {
            "path": ".".join(path),
            "values": _union_json_lists([existing], [incoming]),
        }
    ]


def _list_value(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _union_json_lists(first: list[Any], second: list[Any]) -> list[Any]:
    values = {_json_key(value): value for value in [*first, *second]}
    return [values[key] for key in sorted(values)]


def _json_key(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
