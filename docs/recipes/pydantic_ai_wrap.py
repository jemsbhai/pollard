"""Ledger one complete pydantic-ai run as a governed Pollard model call."""

import sys
from typing import Any

from pollard import Budget, Runtime


def main() -> None:
    from openai import AsyncOpenAI
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    model = OpenAIResponsesModel(
        "gpt-5.5",
        provider=OpenAIProvider(openai_client=AsyncOpenAI(max_retries=0)),
    )
    agent = Agent(
        model,
        retries=0,
        model_settings={"max_tokens": 128, "openai_reasoning_effort": "none"},
    )

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
        "pydantic-ai", budget=Budget(tokens=2_000, steps=2)
    ) as run:
        node = run.model_call(
            {"model": "openai:gpt-5.5", "prompt": "Explain content addressing."},
            fn=call_agent,
        )
        print(node.result["text"])
        print("root:", run.root_id)


if __name__ == "__main__":
    main()
