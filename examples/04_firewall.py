"""Prove that an unknown action is refused before any handler executes."""

from pollard import ActionSpec, MemoryStore, PolicyViolation, Registry, Runtime

executed = False


def approved(args: dict[str, object]) -> dict[str, object]:
    global executed
    executed = True
    return {"ok": True, "text": args["text"], "usage": {"input_tokens": 0, "output_tokens": 0}}


def main() -> None:
    registry = Registry(
        [
            ActionSpec(
                "approved",
                "1",
                "Approved echo action.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                False,
                approved,
            )
        ]
    )

    run = Runtime(MemoryStore(), registry=registry).run("firewall")
    try:
        run.tool_call("delete_everything", {"path": "C:/important"})
    except PolicyViolation as exc:
        refusal = run.store.get(exc.refusal_id)
        print(f"blocked={refusal.payload['reason']} detail={refusal.payload['detail']}")
        print(f"executed={executed}")


if __name__ == "__main__":
    main()
