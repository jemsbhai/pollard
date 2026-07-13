"""A governed call to an Azure OpenAI v1 endpoint."""

import os
import sys

from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn


def main() -> None:
    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    client = OpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        base_url=f"{endpoint}/openai/v1/",
        max_retries=0,
    )
    runtime = Runtime("azure-openai.db", mode="hybrid")
    with runtime.run("azure-openai", budget=Budget(tokens=2_000, steps=2)) as run:
        node = run.model_call(
            {
                "_pollard": {"provider": "azure.ai.openai"},
                "model": deployment,
                "input": "Explain content addressing in two sentences.",
                "max_output_tokens": 128,
            },
            fn=make_responses_fn(client),
        )
        print(node.result["text"])
        print("inspect:", f"pollard show azure-openai.db {run.root_id}")


if __name__ == "__main__":
    main()
