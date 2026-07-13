"""A governed Amazon Bedrock Converse call using a caller-owned boto3 client."""

import argparse
import sys

from pollard import Budget, Runtime
from pollard.adapters.bedrock import make_converse_fn
from pollard.meters import DepthMeter, StepMeter, TokenMeter, WallClockMeter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_id", help="Bedrock model or inference-profile id")
    parser.add_argument("--region", default=None, help="AWS Region; defaults to the SDK chain")
    parser.add_argument(
        "--count-tokens",
        action="store_true",
        help="make a separate Bedrock CountTokens request during precheck",
    )
    parser.add_argument(
        "--database", default="bedrock-converse.db", help="SQLite recording path"
    )
    args = parser.parse_args()

    import boto3

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = boto3.client("bedrock-runtime", region_name=args.region)
    call_bedrock = make_converse_fn(client, count_tokens=args.count_tokens)
    runtime = Runtime(
        args.database,
        meters=[
            StepMeter(),
            DepthMeter(),
            WallClockMeter(),
            TokenMeter(call_bedrock, reserved_output_tokens=128),
        ],
        mode="hybrid",
    )
    with runtime.run("bedrock-converse", budget=Budget(tokens=2_000, steps=2)) as run:
        node = run.model_call(
            {
                "_pollard": {"provider": "aws.bedrock"},
                "modelId": args.model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": "Explain content addressing in two sentences."}],
                    }
                ],
                "inferenceConfig": {"maxTokens": 128},
            },
            fn=call_bedrock,
        )
        print(node.result["text"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
