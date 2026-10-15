"""Record and replay a synthetic Codex task without launching Codex or a model."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from pollard import Budget, MemoryStore, Runtime, verify


def offline_codex(_payload: dict[str, Any]) -> dict[str, Any]:
    # Synthetic data demonstrates the adapter's normalized result contract.
    return {
        "text": "Review complete: the fixture has no findings.",
        "thread_id": "offline-codex-fixture",
        "usage": {"input_tokens": 12, "output_tokens": 8},
        "provider_usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 8},
    }


def must_not_execute(_payload: dict[str, Any]) -> NoReturn:
    raise AssertionError("replay attempted to launch Codex")


def main() -> None:
    store = MemoryStore()
    payload = {
        "prompt": "Review the fixture and report findings.",
        "cwd": "/offline-fixture",
        "model": "offline-codex",
        "sandbox": "read-only",
        "workspace_fingerprint": "fixture-v1-with-config-v1",
    }
    with Runtime(store, refuse_duplicate_recordings=True).run(
        "codex-example",
        budget=Budget(steps=1, tokens=100),
    ) as recording:
        recorded = recording.model_call(payload, fn=offline_codex)
        spent = recording.report()["spent"]
    with Runtime(store, mode="replay").run("codex-example") as replay:
        replayed = replay.model_call(payload, fn=must_not_execute)
        assert recorded.result == replayed.result
        assert verify(store, recorded.id).ok
        print(
            json.dumps(
                {
                    "replay_matched": True,
                    "codex_launches": 0,
                    "spent": spent,
                    "avoided": replay.report()["avoided"],
                }
            )
        )


if __name__ == "__main__":
    main()
