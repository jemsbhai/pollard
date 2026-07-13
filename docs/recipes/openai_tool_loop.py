"""A live OpenAI Responses tool loop behind Pollard's registry firewall."""

import json
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
    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = OpenAI(max_retries=0)
    registry = Registry(
        [ActionSpec("weather", "1", "Fixed demo forecast.", WEATHER_SCHEMA, False, weather)]
    )
    runtime = Runtime("openai-tool-loop.db", registry=registry, mode="hybrid")
    call_openai = make_responses_fn(client)
    input_items: list[dict[str, object]] = [
        {"role": "user", "content": "What is the weather in Boston?"}
    ]

    with runtime.run("openai-tool-loop", budget=Budget(tokens=2_000, steps=6)) as run:
        first = run.model_call(
            {
                "model": "gpt-5.5",
                "input": input_items,
                "tools": OPENAI_TOOLS,
                "max_output_tokens": 128,
                "reasoning": {"effort": "none"},
            },
            fn=call_openai,
        )
        for requested in first.result.get("tool_calls", []):
            args = json.loads(requested["arguments"])
            tool = run.tool_call(requested["name"], args, version="1")
            output = first.result.get("output", [])
            if isinstance(output, list):
                input_items.extend(output)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": requested["call_id"],
                    "output": json.dumps(tool.result),
                }
            )
        final = run.model_call(
            {
                "model": "gpt-5.5",
                "input": input_items,
                "tools": OPENAI_TOOLS,
                "max_output_tokens": 128,
                "reasoning": {"effort": "none"},
            },
            fn=call_openai,
        )
        print(final.result["text"])
        print("root:", run.root_id)


if __name__ == "__main__":
    main()
