"""Record a deterministic stream, then replay every retained chunk offline."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pollard import MemoryStore, Runtime

PAYLOAD = {"model": "mock-1", "input": "Stream a greeting."}


def stream(_payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield {"delta": {"text": "hello"}}
    yield {"delta": {"text": " from Pollard"}}
    yield {
        "result": {
            "text": "hello from Pollard",
            "usage": {"input_tokens": 4, "output_tokens": 3},
        }
    }


def unavailable_client(_payload: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("strict replay must not call the model function")


def main() -> None:
    store = MemoryStore()
    live_chunks: list[dict[str, Any]] = []
    with Runtime(store, mode="record").run("streaming-replay") as run:
        recorded = run.model_call(
            PAYLOAD,
            fn=stream,
            on_delta=live_chunks.append,
            keep_chunks=True,
        )

    replay_chunks: list[dict[str, Any]] = []
    with Runtime(store, mode="replay").run("streaming-replay") as run:
        replayed = run.model_call(
            PAYLOAD,
            fn=unavailable_client,
            on_delta=replay_chunks.append,
        )
        avoided = run.report()["avoided"]

    assert recorded.id == replayed.id
    assert live_chunks == replay_chunks == replayed.result["chunks"]
    print(f"text={replayed.result['text']}")
    print(f"chunks=live:{len(live_chunks)} replay:{len(replay_chunks)}")
    print(f"avoided={avoided}")


if __name__ == "__main__":
    main()
