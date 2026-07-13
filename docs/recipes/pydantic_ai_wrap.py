"""Ledger one complete pydantic-ai run as a governed Pollard model call."""

from typing import Any

from pollard import Budget, Runtime


def main() -> None:
    from pydantic_ai import Agent

    agent = Agent("openai:gpt-5.5")

    def call_agent(payload: dict[str, Any]) -> dict[str, Any]:
        result = agent.run_sync(payload["prompt"])
        return {
            "text": str(result.output),
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        }

    with Runtime("pydantic-ai.db", mode="hybrid").run(
        "pydantic-ai", budget=Budget(tokens=20_000, steps=2)
    ) as run:
        node = run.model_call(
            {"model": "openai:gpt-5.5", "prompt": "Explain content addressing."},
            fn=call_agent,
        )
        print(node.result["text"])
        print("root:", run.root_id)


if __name__ == "__main__":
    main()
