"""Ledger one capped pydantic-ai run as a governed Pollard model call."""

import argparse
import os
import sys
from typing import Any

from pollard import Budget, Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("POLLARD_OPENAI_MODEL", "gpt-5.6"),
        help="OpenAI model ID; defaults to POLLARD_OPENAI_MODEL or gpt-5.6",
    )
    parser.add_argument(
        "--database", default="pydantic-ai.db", help="SQLite recording path"
    )
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set before a live run")

    from openai import AsyncOpenAI
    from pydantic_ai import Agent, UsageLimits
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    model = OpenAIResponsesModel(
        args.model,
        provider=OpenAIProvider(openai_client=AsyncOpenAI(max_retries=0)),
    )
    agent = Agent(
        model,
        retries=0,
        model_settings={
            "max_tokens": 128,
            "openai_reasoning_effort": "none",
            "openai_store": False,
        },
    )

    def call_agent(payload: dict[str, Any]) -> dict[str, Any]:
        result = agent.run_sync(
            payload["prompt"],
            usage_limits=UsageLimits(request_limit=1, output_tokens_limit=128),
        )
        return {
            "text": str(result.output),
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        }

    with Runtime(args.database, mode="hybrid").run(
        "pydantic-ai", budget=Budget(tokens=2_000, steps=2)
    ) as run:
        node = run.model_call(
            {"model": f"openai:{args.model}", "prompt": "Explain content addressing."},
            fn=call_agent,
        )
        print(node.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
