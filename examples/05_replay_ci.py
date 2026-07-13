"""Replay-mode example intended for CI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pollard import Runtime

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDING = ROOT / "tests" / "pollard_recordings" / "test_replay_ci.db"
RUN_LABEL = "replay-ci-example"
PAYLOAD: dict[str, Any] = {
    "model": "mock-1",
    "messages": [{"role": "user", "content": "Replay this offline."}],
}


def run_replay(recording_path: Path = DEFAULT_RECORDING) -> dict[str, Any]:
    with Runtime(recording_path, mode="replay").run(RUN_LABEL) as run:
        node = run.model_call(PAYLOAD, fn=_live_client)
        return {
            "text": node.result["text"],
            "avoided": run.report()["avoided"],
        }


def main() -> None:
    result = run_replay()
    print(result["text"])
    print(f"avoided={result['avoided']}")


def _live_client(_payload: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("replay mode should not call the live client")


if __name__ == "__main__":
    main()
