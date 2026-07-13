"""Run a cloud-hosted model through LiteLLM and Pollard."""

import argparse
import sys

from pollard import Budget, Runtime
from pollard.adapters.litellm import make_completion_fn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="LiteLLM route, such as vertex_ai/gemini-2.5-flash")
    parser.add_argument(
        "--provider",
        default="unknown",
        help="OpenTelemetry provider name, such as gcp.vertex_ai",
    )
    parser.add_argument(
        "--database", default="litellm-cloud.db", help="SQLite recording path"
    )
    args = parser.parse_args()

    from litellm import completion

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    call_model = make_completion_fn(completion, num_retries=0, max_tokens=128)
    with Runtime(args.database, mode="hybrid").run(
        "litellm-cloud", budget=Budget(tokens=2_000, steps=2)
    ) as run:
        node = run.model_call(
            {
                "_pollard": {"provider": args.provider},
                "model": args.model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Explain content addressing in two sentences.",
                    }
                ],
            },
            fn=call_model,
        )
        print(node.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
