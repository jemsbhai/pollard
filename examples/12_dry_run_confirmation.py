"""Preview a side effect in dry-run mode, then require confirmation to execute."""

from __future__ import annotations

from typing import Any

from pollard import (
    ActionSpec,
    ConfirmationRequired,
    Decision,
    MemoryStore,
    PolicyContext,
    Registry,
    Runtime,
)


class ConfirmSideEffects:
    def decide(self, context: PolicyContext) -> Decision:
        if context.spec.side_effects:
            return Decision.CONFIRM
        return Decision.ALLOW


def main() -> None:
    deliveries: list[str] = []

    def deliver(args: dict[str, Any]) -> dict[str, Any]:
        deliveries.append(str(args["recipient"]))
        return {
            "queued": True,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    registry = Registry(
        [
            ActionSpec(
                "deliver",
                "1",
                "Queue a message for delivery.",
                {
                    "type": "object",
                    "properties": {
                        "recipient": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["recipient", "message"],
                    "additionalProperties": False,
                },
                True,
                deliver,
            )
        ]
    )
    args = {"recipient": "operator@example.test", "message": "Ready for review."}

    preview = Runtime(MemoryStore(), registry=registry, dry_run=True).run("preview")
    preview_node = preview.tool_call("deliver", args)
    assert preview_node.meta["dry_run"] is True
    assert deliveries == []

    governed = Runtime(
        MemoryStore(),
        registry=registry,
        policies=[ConfirmSideEffects()],
    ).run("confirmed")
    try:
        governed.tool_call("deliver", args)
    except ConfirmationRequired as exc:
        assert deliveries == []
        confirmed = governed.confirm(exc.resume_token)
    else:
        raise AssertionError("side effect should pause for confirmation")

    assert confirmed.result["queued"] is True
    assert deliveries == ["operator@example.test"]
    print("dry_run_executed=false")
    print("confirmation_paused=true")
    print("confirmed_executed=true")


if __name__ == "__main__":
    main()
