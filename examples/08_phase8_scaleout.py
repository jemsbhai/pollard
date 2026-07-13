"""Run the Phase 8 merge and shared-window demo without network access."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pollard import BudgetExceeded, Runtime, SQLiteStore, WindowMeter, merge, verify


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pollard-phase8-") as temporary:
        directory = Path(temporary)
        shared_path = directory / "shared.db"
        with SQLiteStore(shared_path) as store:
            run = Runtime(
                store, meters=[WindowMeter("requests", 1, 60)]
            ).run("shared-window")
            run.model_call({"model": "mock", "index": 1}, fn=lambda _payload: {})
        with SQLiteStore(shared_path) as store:
            resumed = Runtime(
                store, meters=[WindowMeter("requests", 1, 60)]
            ).run("shared-window")
            try:
                resumed.model_call(
                    {"model": "mock", "index": 2}, fn=lambda _payload: {}
                )
            except BudgetExceeded as exc:
                refusal = store.get(exc.refusal_id)

        first_path = directory / "first.db"
        second_path = directory / "second.db"
        destination_path = directory / "combined.db"
        with SQLiteStore(first_path) as first:
            Runtime(first).run("first").note({"worker": "a"})
        with SQLiteStore(second_path) as second:
            Runtime(second).run("second").note({"worker": "b"})
        with (
            SQLiteStore(destination_path) as destination,
            SQLiteStore(first_path) as first,
            SQLiteStore(second_path) as second,
        ):
            first_report = merge(destination, first)
            second_report = merge(destination, second)
            clean = all(verify(destination, root_id).ok for root_id in destination.roots())

        print(
            json.dumps(
                {
                    "window_refusal": refusal.payload,
                    "merged_nodes": first_report.copied + second_report.copied,
                    "verify_clean": clean,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
