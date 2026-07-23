"""Redact a sensitive registry argument while passing plaintext to its handler."""

from __future__ import annotations

import json
from typing import Any

from pollard import ActionSpec, MemoryStore, Registry, Runtime

SECRET = "example-token-that-must-not-be-stored"


def main() -> None:
    received: list[str] = []

    def submit(args: dict[str, Any]) -> dict[str, Any]:
        received.append(str(args["token"]))
        return {
            "accepted": True,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    registry = Registry(
        [
            ActionSpec(
                "submit",
                "1",
                "Submit a value using a protected credential.",
                {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "sensitive": True},
                        "value": {"type": "string"},
                    },
                    "required": ["token", "value"],
                    "additionalProperties": False,
                },
                True,
                submit,
            )
        ]
    )
    store = MemoryStore()
    with Runtime(store, registry=registry).run("sensitive-fields") as run:
        node = run.tool_call("submit", {"token": SECRET, "value": "public"})
        stored_payloads = json.dumps(
            [stored.payload for stored in store.walk(run.root_id)],
            sort_keys=True,
        )

    marker = node.payload["args"]["token"]
    assert received == [SECRET]
    assert SECRET not in stored_payloads
    assert isinstance(marker, dict)
    digest = marker["__pollard_redacted"]
    print("handler_received_plaintext=true")
    print("stored_plaintext=false")
    print(f"redaction_digest={str(digest)[:12]}")


if __name__ == "__main__":
    main()
