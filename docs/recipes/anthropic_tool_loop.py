"""A live Anthropic tool loop with token estimation and registry gating."""

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
    from anthropic import Anthropic

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = Anthropic(max_retries=0)
    call_anthropic = make_messages_fn(client, max_tokens=128)
    runtime = Runtime(
        "anthropic-tool-loop.db",
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
                "model": "claude-sonnet-4-6",
                "messages": messages,
                "tools": ANTHROPIC_TOOLS,
                "output_config": {"effort": "low"},
            },
            fn=call_anthropic,
        )
        messages.append({"role": "assistant", "content": first.result["content"]})
        tool_results: list[dict[str, object]] = []
        for requested in first.result.get("tool_calls", []):
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
                "model": "claude-sonnet-4-6",
                "messages": messages,
                "tools": ANTHROPIC_TOOLS,
                "output_config": {"effort": "low"},
            },
            fn=call_anthropic,
        )
        print(final.result["text"])
        print("inspect:", f"pollard show anthropic-tool-loop.db {run.root_id}")


if __name__ == "__main__":
    main()
