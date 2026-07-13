"""Run the formal offline EXP-004 SQLite storage-curve experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pollard
from pollard import SQLiteStore
from pollard.tree import Node, NodeKind

SEEDS = tuple(range(5))
CHECKPOINTS = (25, 50, 100, 200)
MESSAGE_BYTES = 8192
INTERN_THRESHOLD = 1024
T_95_DF4 = 2.776


def message_content(seed: int, turn: int) -> str:
    digest = hashlib.sha256(f"pollard-exp-004-{seed}-{turn}".encode()).hexdigest()
    return (digest * (MESSAGE_BYTES // len(digest) + 1))[:MESSAGE_BYTES]


def build(
    path: Path,
    turns: int,
    seed: int,
    *,
    intern_payloads: bool,
) -> tuple[int, str, dict[str, str | int]]:
    messages: list[dict[str, str]] = []
    with SQLiteStore(
        path,
        intern_payloads=intern_payloads,
        intern_threshold=INTERN_THRESHOLD,
    ) as store:
        root = Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"experiment": "EXP-004", "seed": seed, "turns": turns},
        )
        store.put(root)
        cursor = root.id
        for turn in range(turns):
            messages.append({"role": "user", "content": message_content(seed, turn)})
            node = Node.make(
                kind=NodeKind.MODEL_CALL,
                parent=cursor,
                payload={"model": "synthetic", "messages": list(messages)},
            )
            store.put(node)
            cursor = node.id
    with closing(sqlite3.connect(path)) as connection:
        pragmas: dict[str, str | int] = {
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            "auto_vacuum": int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]),
        }
    return path.stat().st_size, cursor, pragmas


def fit_curve(points: list[tuple[int, int]]) -> dict[str, float]:
    xs = [math.log(turns) for turns, _size in points]
    ys = [math.log(size) for _turns, size in points]
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    exponent = numerator / denominator
    intercept = y_mean - exponent * x_mean
    fitted = [intercept + exponent * x for x in xs]
    total = sum((y - y_mean) ** 2 for y in ys)
    residual = sum((y - prediction) ** 2 for y, prediction in zip(ys, fitted, strict=True))
    r_squared = 1.0 if total == 0 else 1.0 - residual / total
    return {
        "exponent": round(exponent, 6),
        "intercept": round(intercept, 6),
        "r_squared": round(r_squared, 6),
    }


def mean_ci95(values: list[float]) -> dict[str, float]:
    mean = statistics.mean(values)
    half_width = 0.0
    if len(values) > 1:
        half_width = T_95_DF4 * statistics.stdev(values) / math.sqrt(len(values))
    return {"mean": round(mean, 6), "ci95_half_width": round(half_width, 6)}


def run_experiment() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    pragmas: dict[str, str | int] | None = None
    with tempfile.TemporaryDirectory(prefix="pollard-exp-004-") as temporary:
        directory = Path(temporary)
        for seed in SEEDS:
            for turns in CHECKPOINTS:
                paths = {
                    "interned": directory / f"interned-{seed}-{turns}.db",
                    "plain": directory / f"plain-{seed}-{turns}.db",
                }
                built: dict[str, tuple[int, str, dict[str, str | int]]] = {}
                for condition, path in paths.items():
                    built[condition] = build(
                        path,
                        turns,
                        seed,
                        intern_payloads=condition == "interned",
                    )
                interned_bytes, interned_id, interned_pragmas = built["interned"]
                plain_bytes, plain_id, plain_pragmas = built["plain"]
                if interned_id != plain_id:
                    raise AssertionError(f"node identity changed for seed={seed}, turns={turns}")
                if interned_pragmas != plain_pragmas:
                    raise AssertionError("SQLite pragmas differ between conditions")
                if pragmas is None:
                    pragmas = interned_pragmas
                elif pragmas != interned_pragmas:
                    raise AssertionError("SQLite pragmas changed during the experiment")
                rows.append(
                    {
                        "seed": seed,
                        "turns": turns,
                        "interned_bytes": interned_bytes,
                        "plain_bytes": plain_bytes,
                        "plain_to_interned_ratio": round(plain_bytes / interned_bytes, 6),
                        "identity_parity": True,
                    }
                )
                for path in paths.values():
                    path.unlink()

    fits: dict[str, list[dict[str, Any]]] = {"interned": [], "plain": []}
    for seed in SEEDS:
        selected = [row for row in rows if row["seed"] == seed]
        for condition in fits:
            points = [
                (int(row["turns"]), int(row[f"{condition}_bytes"]))
                for row in selected
            ]
            fits[condition].append({"seed": seed, **fit_curve(points)})

    checkpoint_summary: list[dict[str, Any]] = []
    for turns in CHECKPOINTS:
        selected = [row for row in rows if row["turns"] == turns]
        checkpoint_summary.append(
            {
                "turns": turns,
                "interned_bytes": mean_ci95(
                    [float(row["interned_bytes"]) for row in selected]
                ),
                "plain_bytes": mean_ci95([float(row["plain_bytes"]) for row in selected]),
                "plain_to_interned_ratio": mean_ci95(
                    [float(row["plain_to_interned_ratio"]) for row in selected]
                ),
            }
        )

    exponent_summary = {
        condition: mean_ci95([float(row["exponent"]) for row in condition_fits])
        for condition, condition_fits in fits.items()
    }
    passed = (
        all(row["identity_parity"] for row in rows)
        and all(row["interned_bytes"] < row["plain_bytes"] for row in rows)
        and all(
            float(interned["exponent"]) < float(plain["exponent"])
            for interned, plain in zip(fits["interned"], fits["plain"], strict=True)
        )
    )
    return {
        "id": "EXP-004",
        "status": "passed" if passed else "failed",
        "question": (
            "How do 200-turn SQLite storage curves differ with payload interning on and off?"
        ),
        "protocol": {
            "seeds": list(SEEDS),
            "checkpoints": list(CHECKPOINTS),
            "message_bytes_per_turn": MESSAGE_BYTES,
            "intern_threshold_bytes": INTERN_THRESHOLD,
            "fit": "ordinary least squares over ln(turns) and ln(closed database bytes)",
            "confidence_interval": "two-sided 95% Student t interval, df=4",
            "scope_limit": "fitted finite-range curves; no asymptotic complexity claim",
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "pollard": pollard.__version__,
            "pragmas": pragmas,
        },
        "rows": rows,
        "fits": fits,
        "summary": {
            "checkpoints": checkpoint_summary,
            "fitted_exponents": exponent_summary,
            "all_node_ids_match": all(row["identity_parity"] for row in rows),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON result to this path")
    args = parser.parse_args()
    rendered = json.dumps(run_experiment(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
