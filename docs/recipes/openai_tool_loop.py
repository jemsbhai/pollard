"""Run a capped OpenAI Responses tool loop through Pollard's registry firewall."""

import argparse
import json
import os
import sys

from pollard import ActionSpec, Budget, Registry, Runtime
from pollard.adapters.openai import make_responses_fn

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
OPENAI_TOOLS = [
    {
        "type": "function",
        "name": "weather",
        "description": "Return the fixed demo forecast for a city.",
        "strict": True,
        "parameters": WEATHER_SCHEMA,
    }
]


def weather(args: dict[str, object]) -> dict[str, object]:
    return {
        "city": args["city"],
        "forecast": "sunny",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("POLLARD_OPENAI_MODEL", "gpt-5.6"),
        help="OpenAI model ID; defaults to POLLARD_OPENAI_MODEL or gpt-5.6",
    )
    parser.add_argument(
        "--database",
        default="openai-tool-loop.db",
        help="SQLite recording path",
    )
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set before a live run")

    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = OpenAI(max_retries=0)
    registry = Registry(
        [ActionSpec("weather", "1", "Fixed demo forecast.", WEATHER_SCHEMA, False, weather)]
    )
    runtime = Runtime(args.database, registry=registry, mode="hybrid")
    call_openai = make_responses_fn(client, store=False)
    input_items: list[dict[str, object]] = [
        {"role": "user", "content": "What is the weather in Boston?"}
    ]

    with runtime.run("openai-tool-loop", budget=Budget(tokens=2_000, steps=6)) as run:
        first = run.model_call(
            {
                "model": args.model,
                "input": input_items,
                "tools": OPENAI_TOOLS,
                "tool_choice": {"type": "function", "name": "weather"},
                "parallel_tool_calls": False,
                "max_output_tokens": 128,
                "reasoning": {"effort": "none"},
            },
            fn=call_openai,
        )
        requested_tools = first.result.get("tool_calls", [])
        if not isinstance(requested_tools, list) or len(requested_tools) != 1:
            raise RuntimeError("expected exactly one weather tool call")
        output = first.result.get("output", [])
        if isinstance(output, list):
            input_items.extend(output)
        for requested in requested_tools:
            tool_args = json.loads(requested["arguments"])
            tool = run.tool_call(requested["name"], tool_args, version="1")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": requested["call_id"],
                    "output": json.dumps(tool.result),
                }
            )
        final = run.model_call(
            {
                "model": args.model,
                "input": input_items,
                "tools": OPENAI_TOOLS,
                "tool_choice": "none",
                "max_output_tokens": 128,
                "reasoning": {"effort": "none"},
            },
            fn=call_openai,
        )
        print(final.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
