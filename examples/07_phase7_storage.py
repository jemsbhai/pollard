"""Measure the Phase 7 synthetic storage-growth checkpoint."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from pollard import SQLiteStore
from pollard.tree import Node, NodeKind

CHECKPOINTS = (25, 50, 100, 200)
MESSAGE_BYTES = 8192


def message_content(turn: int) -> str:
    seed = hashlib.sha256(f"pollard-phase7-turn-{turn}".encode()).hexdigest()
    return (seed * (MESSAGE_BYTES // len(seed) + 1))[:MESSAGE_BYTES]


def build(path: Path, turns: int, *, intern_payloads: bool) -> tuple[int, str]:
    messages: list[dict[str, str]] = []
    with SQLiteStore(path, intern_payloads=intern_payloads) as store:
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": f"phase7-storage-{turns}"},
        )
        store.put(root)
        cursor = root.id
        for turn in range(turns):
            messages.append({"role": "user", "content": message_content(turn)})
            node = Node.make(
                kind=NodeKind.MODEL_CALL,
                parent=cursor,
                payload={"model": "synthetic", "messages": list(messages)},
            )
            store.put(node)
            cursor = node.id
    return path.stat().st_size, cursor


def fitted_exponent(points: list[dict[str, Any]]) -> float:
    xs = [math.log(int(point["turns"])) for point in points]
    ys = [math.log(int(point["bytes"])) for point in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return numerator / denominator


def main() -> None:
    points: dict[str, list[dict[str, Any]]] = {"interned": [], "plain": []}
    with tempfile.TemporaryDirectory(prefix="pollard-phase7-") as temporary:
        directory = Path(temporary)
        for turns in CHECKPOINTS:
            interned_bytes, interned_id = build(
                directory / f"interned-{turns}.db", turns, intern_payloads=True
            )
            plain_bytes, plain_id = build(
                directory / f"plain-{turns}.db", turns, intern_payloads=False
            )
            if interned_id != plain_id:
                raise AssertionError(f"node identity changed at {turns} turns")
            points["interned"].append({"turns": turns, "bytes": interned_bytes})
            points["plain"].append({"turns": turns, "bytes": plain_bytes})
    final_interned = int(points["interned"][-1]["bytes"])
    final_plain = int(points["plain"][-1]["bytes"])
    document = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "message_bytes": MESSAGE_BYTES,
        "intern_threshold": 1024,
        "points": points,
        "fitted_exponent": {
            name: fitted_exponent(values) for name, values in points.items()
        },
        "plain_to_interned_ratio_at_200": final_plain / final_interned,
        "identity_parity": True,
    }
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
