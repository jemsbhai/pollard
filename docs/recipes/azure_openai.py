"""Run one capped, governed call against an Azure OpenAI v1 deployment."""

import argparse
import os
import sys

from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", default="azure-openai.db", help="SQLite recording path"
    )
    parser.add_argument(
        "--entra-id",
        action="store_true",
        help="authenticate with DefaultAzureCredential instead of AZURE_OPENAI_API_KEY",
    )
    args = parser.parse_args()
    missing = [
        name
        for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
        if not os.getenv(name)
    ]
    if not args.entra_id and not os.getenv("AZURE_OPENAI_API_KEY"):
        missing.append("AZURE_OPENAI_API_KEY")
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")

    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    if args.entra_id:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        credential = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://ai.azure.com/.default",
        )
    else:
        credential = os.environ["AZURE_OPENAI_API_KEY"]
    client = OpenAI(
        api_key=credential,
        base_url=f"{endpoint}/openai/v1/",
        max_retries=0,
    )
    runtime = Runtime(args.database, mode="hybrid")
    with runtime.run("azure-openai", budget=Budget(tokens=2_000, steps=2)) as run:
        node = run.model_call(
            {
                "_pollard": {"provider": "azure.ai.openai"},
                "model": deployment,
                "input": "Explain content addressing in two sentences.",
                "max_output_tokens": 128,
            },
            fn=make_responses_fn(client, store=False),
        )
        print(node.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
