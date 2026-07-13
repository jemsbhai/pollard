"""Run a capped Anthropic tool loop with token prechecks and registry gating."""

import argparse
import os
import sys

from pollard import ActionSpec, Budget, Registry, Runtime
from pollard.adapters.anthropic import make_messages_fn
from pollard.meters import DepthMeter, StepMeter, TokenMeter, WallClockMeter

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
ANTHROPIC_TOOLS = [
    {
        "name": "weather",
        "description": "Return the fixed demo forecast for a city.",
        "strict": True,
        "input_schema": WEATHER_SCHEMA,
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
        default=os.getenv("POLLARD_ANTHROPIC_MODEL", "claude-sonnet-5"),
        help="Claude model ID; defaults to POLLARD_ANTHROPIC_MODEL or claude-sonnet-5",
    )
    parser.add_argument(
        "--database",
        default="anthropic-tool-loop.db",
        help="SQLite recording path",
    )
    args = parser.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        parser.error("ANTHROPIC_API_KEY must be set before a live run")

    from anthropic import Anthropic

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = Anthropic(max_retries=0)
    call_anthropic = make_messages_fn(client, max_tokens=128)
    runtime = Runtime(
        args.database,
        registry=Registry(
            [ActionSpec("weather", "1", "Fixed demo forecast.", WEATHER_SCHEMA, False, weather)]
        ),
        meters=[
            StepMeter(),
            DepthMeter(),
            WallClockMeter(),
            TokenMeter(call_anthropic, reserved_output_tokens=128),
        ],
        mode="hybrid",
    )
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "What is the weather in Boston?"}
    ]

    with runtime.run("anthropic-tool-loop", budget=Budget(tokens=2_000, steps=6)) as run:
        first = run.model_call(
            {
                "model": args.model,
                "messages": messages,
                "tools": ANTHROPIC_TOOLS,
                "tool_choice": {"type": "tool", "name": "weather"},
                "output_config": {"effort": "low"},
            },
            fn=call_anthropic,
        )
        requested_tools = first.result.get("tool_calls", [])
        if not isinstance(requested_tools, list) or len(requested_tools) != 1:
            raise RuntimeError("expected exactly one weather tool call")
        messages.append({"role": "assistant", "content": first.result["content"]})
        tool_results: list[dict[str, object]] = []
        for requested in requested_tools:
            tool = run.tool_call(requested["name"], requested["input"], version="1")
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": requested["id"],
                    "content": str(tool.result),
                }
            )
        messages.append({"role": "user", "content": tool_results})
        final = run.model_call(
            {
                "model": args.model,
                "messages": messages,
                "tools": ANTHROPIC_TOOLS,
                "tool_choice": {"type": "none"},
                "output_config": {"effort": "low"},
            },
            fn=call_anthropic,
        )
        print(final.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
