"""Conflict-aware union of append-only Pollard stores."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._canon import canonical_bytes
from .errors import IntegrityError
from .store import Store, _validate_for_put
from .tree import Node

_SPOOL_VERSION = "1"
_SPOOL_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE nodes (
    position INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE,
    record BLOB NOT NULL,
    digest TEXT NOT NULL
);
"""
_SPOOL_METADATA_KEYS = frozenset({"version", "state", "count", "digest"})


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


@dataclass(frozen=True)
class _MergePlan:
    incoming_nodes: tuple[Node, ...]

    def iter_nodes(self) -> Iterator[Node]:
        return iter(self.incoming_nodes)


@dataclass(frozen=True)
class _MergeSpool:
    """A finalized, disk-backed merge plan."""

    path: Path
    count: int
    digest: str

    @classmethod
    def prepare(cls, path: Path, src: Store) -> _MergeSpool:
        digest = hashlib.sha256()
        count = 0
        try:
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA cache_size = -1024")
                connection.execute("PRAGMA temp_store = MEMORY")
                connection.executescript(_SPOOL_SCHEMA)
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (("version", _SPOOL_VERSION), ("state", "writing")),
                )
                for root_id in src.roots():
                    yielded_root = False
                    for incoming in src.walk(root_id):
                        if not yielded_root:
                            if incoming.id != root_id or incoming.parent is not None:
                                raise IntegrityError(
                                    "merge source traversal did not begin with "
                                    "its declared root"
                                )
                            yielded_root = True
                        record = _node_record(incoming)
                        if (
                            incoming.parent is not None
                            and connection.execute(
                                "SELECT 1 FROM nodes WHERE node_id = ?",
                                (incoming.parent,),
                            ).fetchone()
                            is None
                        ):
                            raise IntegrityError(
                                "merge source traversal yielded a node before its parent"
                            )
                        record_digest = hashlib.sha256(record).hexdigest()
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO nodes
                                (position, node_id, record, digest)
                            VALUES (?, ?, ?, ?)
                            """,
                            (count, incoming.id, record, record_digest),
                        )
                        if cursor.rowcount == 0:
                            continue
                        _update_spool_digest(digest, record)
                        count += 1
                    if not yielded_root:
                        raise IntegrityError(
                            "merge source traversal yielded no declared root"
                        )
                final_digest = digest.hexdigest()
                connection.executemany(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                    (
                        ("state", "complete"),
                        ("count", str(count)),
                        ("digest", final_digest),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise OSError("could not safely materialize merge source") from exc
        spool = cls(path=path, count=count, digest=final_digest)
        spool.validate()
        return spool

    def validate(self) -> None:
        connection = self._open_read_only()
        try:
            self._validate_connection(connection)
        finally:
            _close_spool_connection(connection)

    def iter_nodes(self) -> Iterator[Node]:
        connection = self._open_read_only()
        try:
            connection.execute("BEGIN")
            self._validate_connection(connection)
            for _position, _node_id, record, _digest in connection.execute(
                """
                SELECT position, node_id, record, digest
                FROM nodes
                ORDER BY position
                """
            ):
                yield _node_from_record(_record_bytes(record))
        finally:
            _close_spool_connection(connection)

    def _open_read_only(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise IntegrityError("prepared merge spool is missing")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA cache_size = -1024")
            connection.execute("PRAGMA temp_store = MEMORY")
            return connection
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                _close_spool_connection(connection)
            raise IntegrityError("prepared merge spool cannot be opened") from exc

    def _validate_connection(self, connection: sqlite3.Connection) -> None:
        try:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity_rows != [("ok",)]:
                raise IntegrityError("prepared merge spool failed integrity validation")
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            if self.path.stat().st_size != page_count * page_size:
                raise IntegrityError("prepared merge spool has trailing or missing data")
            objects = connection.execute(
                """
                SELECT type, name
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            if objects != [("table", "metadata"), ("table", "nodes")]:
                raise IntegrityError("prepared merge spool has an invalid schema")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if set(metadata) != _SPOOL_METADATA_KEYS:
                raise IntegrityError("prepared merge spool has invalid metadata")
            if metadata["version"] != _SPOOL_VERSION:
                raise IntegrityError("prepared merge spool has an unsupported version")
            if metadata["state"] != "complete":
                raise IntegrityError("prepared merge spool is not finalized")
            expected_count = _spool_count(metadata["count"])
            expected_digest = metadata["digest"]
            if not _is_digest(expected_digest):
                raise IntegrityError("prepared merge spool has an invalid digest")
            if expected_count != self.count or expected_digest != self.digest:
                raise IntegrityError("prepared merge spool manifest mismatch")

            digest = hashlib.sha256()
            count = 0
            for position, node_id, record_value, record_digest in connection.execute(
                """
                SELECT position, node_id, record, digest
                FROM nodes
                ORDER BY position
                """
            ):
                if position != count:
                    raise IntegrityError("prepared merge spool ordering is invalid")
                record = _record_bytes(record_value)
                if not isinstance(node_id, str) or not isinstance(record_digest, str):
                    raise IntegrityError("prepared merge spool has invalid node fields")
                if hashlib.sha256(record).hexdigest() != record_digest:
                    raise IntegrityError("prepared merge spool record digest mismatch")
                node = _node_from_record(record)
                if node.id != node_id:
                    raise IntegrityError("prepared merge spool node id mismatch")
                _update_spool_digest(digest, record)
                count += 1
            if count != expected_count or digest.hexdigest() != expected_digest:
                raise IntegrityError("prepared merge spool is corrupt or truncated")
        except IntegrityError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise IntegrityError("prepared merge spool is corrupt or truncated") from exc


def _prepare_merge(src: Store) -> _MergePlan:
    seen: set[str] = set()
    incoming_nodes: list[Node] = []
    for root_id in src.roots():
        for incoming in src.walk(root_id):
            _validate_for_put(incoming)
            if incoming.id in seen:
                continue
            seen.add(incoming.id)
            incoming_nodes.append(incoming)
    return _MergePlan(tuple(incoming_nodes))


def merge(dst: Store, src: Store, *, replay: bool = False) -> MergeReport:
    """Union every node in ``src`` into ``dst`` without discarding metadata.

    Identity collisions are integrity failures. A result collision keeps the
    destination result and records the incoming result, unless ``replay`` is
    true, where nondeterministic results are rejected.
    """

    return _merge_prepared(dst, _prepare_merge(src), replay=replay)


def _merge_prepared(
    dst: Store,
    plan: _MergePlan | _MergeSpool,
    *,
    replay: bool = False,
) -> MergeReport:
    copied = 0
    existing_count = 0
    result_conflict_count = 0
    meta_conflict_count = 0
    for incoming in plan.iter_nodes():
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

    for incoming in plan.iter_nodes():
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


def _node_record(node: Node) -> bytes:
    _validate_for_put(node)
    _validate_node_round_trip(node)
    return _encode_node_record(node)


def _encode_node_record(node: Node) -> bytes:
    return _json_key(
        {
            "attempt": node.attempt,
            "id": node.id,
            "kind": node.kind,
            "meta": _json_text(node.meta),
            "parent": node.parent,
            "payload": canonical_bytes(node.payload).decode("utf-8"),
            "result": node.result_text,
            "result_digest": node.result_digest,
            "version": 1,
        }
    ).encode("utf-8")


def _validate_node_round_trip(node: Node) -> None:
    try:
        payload = json.loads(
            canonical_bytes(node.payload),
            parse_constant=_reject_json_constant,
        )
        meta = json.loads(
            _json_text(node.meta),
            parse_constant=_reject_json_constant,
        )
        result = (
            None
            if node.result_text is None
            else json.loads(
                node.result_text,
                parse_constant=_reject_json_constant,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntegrityError(
            "merge source node cannot be serialized without changing it"
        ) from exc
    if (
        not _same_json_value(payload, node.payload)
        or not _same_json_value(meta, node.meta)
        or not _same_json_value(result, node.result)
    ):
        raise IntegrityError(
            "merge source node cannot be serialized without changing it"
        )


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _node_from_record(record: bytes) -> Node:
    try:
        document = json.loads(record)
        if not isinstance(document, dict) or set(document) != {
            "attempt",
            "id",
            "kind",
            "meta",
            "parent",
            "payload",
            "result",
            "result_digest",
            "version",
        }:
            raise TypeError("invalid fields")
        if document["version"] != 1 or isinstance(document["version"], bool):
            raise TypeError("invalid version")
        parent = document["parent"]
        result = document["result"]
        result_digest = document["result_digest"]
        if parent is not None and not isinstance(parent, str):
            raise TypeError("invalid parent")
        if result is not None and not isinstance(result, str):
            raise TypeError("invalid result")
        if result_digest is not None and not isinstance(result_digest, str):
            raise TypeError("invalid result digest")
        node = Node.from_storage(
            id=_record_string(document, "id"),
            parent=parent,
            kind=_record_string(document, "kind"),
            attempt=_record_integer(document, "attempt"),
            payload_text=_record_string(document, "payload"),
            result_text=result,
            result_digest=result_digest,
            meta_text=_record_string(document, "meta"),
        )
        _validate_for_put(node)
        _validate_node_round_trip(node)
        if _encode_node_record(node) != record:
            raise TypeError("non-canonical record")
        return node
    except (
        IntegrityError,
        json.JSONDecodeError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise IntegrityError("prepared merge spool contains an invalid node") from exc


def _record_string(document: dict[str, Any], name: str) -> str:
    value = document[name]
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _record_integer(document: dict[str, Any], name: str) -> int:
    value = document[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return int(value)


def _same_json_value(first: Any, second: Any) -> bool:
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return set(first) == set(second) and all(
            _same_json_value(first[key], second[key]) for key in first
        )
    if isinstance(first, list):
        return len(first) == len(second) and all(
            _same_json_value(left, right)
            for left, right in zip(first, second, strict=True)
        )
    return _json_key(first) == _json_key(second)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _record_bytes(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise IntegrityError("prepared merge spool record must be bytes")
    return value


def _close_spool_connection(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except sqlite3.Error as exc:
        raise IntegrityError("prepared merge spool connection could not be closed") from exc


def _spool_count(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise IntegrityError("prepared merge spool has an invalid node count")
    count = int(value)
    if str(count) != value:
        raise IntegrityError("prepared merge spool has a non-canonical node count")
    return count


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _update_spool_digest(digest: Any, record: bytes) -> None:
    digest.update(len(record).to_bytes(8, "big"))
    digest.update(record)
