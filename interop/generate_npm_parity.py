"""Generate release-grounded provider/comparator fixtures for the native npm port."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pollard import __version__  # noqa: E402
from pollard.adapters.anthropic import normalize_message  # noqa: E402
from pollard.adapters.bedrock import normalize_converse  # noqa: E402
from pollard.adapters.openai import (  # noqa: E402
    _normalize_response,
    normalize_chat_completion,
)
from pollard.revalidation import (  # noqa: E402
    ExactResultComparator,
    NormalizedModelComparator,
    ReplayContract,
)


def generate() -> dict[str, object]:
    normalizers = {
        "response": _normalize_response,
        "chat": normalize_chat_completion,
        "anthropic": normalize_message,
        "bedrock": normalize_converse,
    }
    examples = [
        (
            "response",
            {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello π"}]},
                    {"type": "function_call", "call_id": "a", "name": "find", "arguments": "{}"},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        ),
        (
            "chat",
            {
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "x",
                                    "type": "function",
                                    "function": {"name": "find", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
            },
        ),
        (
            "anthropic",
            {
                "content": [
                    {"type": "text", "text": "ready"},
                    {"type": "tool_use", "id": "a", "name": "find", "input": {"q": "x"}},
                ],
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 7,
                },
            },
        ),
        (
            "bedrock",
            {
                "output": {
                    "message": {
                        "content": [
                            {"text": "ready"},
                            {"toolUse": {"toolUseId": "a", "name": "find", "input": {"q": "x"}}},
                        ]
                    }
                },
                "usage": {
                    "inputTokens": 2,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 5,
                    "cacheWriteInputTokens": 7,
                },
            },
        ),
        ("chat", {"choices": [], "usage": {"prompt_tokens": -1, "completion_tokens": 3}}),
        (
            "anthropic",
            {"usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": True}},
        ),
        ("bedrock", {"usage": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": -1}}),
    ]
    comparisons = [
        (
            {"text": "ok", "id": "one", "usage": {"input_tokens": 2}},
            {"text": "ok", "id": "two", "usage": {"input_tokens": 3}},
        ),
        (
            {
                "text": "x",
                "tool_calls": [
                    {"id": "a", "function": {"name": "find", "arguments": '{"b":2,"a":1}'}}
                ],
            },
            {
                "text": "x",
                "tool_calls": [
                    {"id": "b", "function": {"name": "find", "arguments": '{"a":1,"b":2}'}}
                ],
            },
        ),
        ({"text": "a", "data": {"slash/key": 1}}, {"text": "b", "data": {"slash/key": 2}}),
    ]
    contract = ReplayContract(
        provider="mock",
        model_revision="rev-1",
        application_revision="app-1",
        environment={"region": "local"},
    )
    return {
        "python_release": __version__,
        "providers": [
            {"kind": kind, "input": value, "expected": normalizers[kind](value)}
            for kind, value in examples
        ],
        "comparisons": [
            {
                "recorded": a,
                "live": b,
                "exact": ExactResultComparator().compare(a, b).to_dict(),
                "normalized": NormalizedModelComparator().compare(a, b).to_dict(),
            }
            for a, b in comparisons
        ],
        "contract": {
            "document": contract.to_dict(),
            "bound": contract.bind({"model": "mock", "prompt": "hi"}),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    path = ROOT / "packages/npm/test/python-parity.json"
    generated = generate()
    if options.check:
        try:
            recorded_text = path.read_text(encoding="utf-8")
            recorded = json.loads(recorded_text)
        except (OSError, ValueError) as exc:
            print(f"cannot read npm parity reference: {type(exc).__name__}", file=sys.stderr)
            return 1
        reference_release = recorded.get("python_release") if isinstance(recorded, dict) else None
        if not isinstance(reference_release, str) or not reference_release:
            print("npm parity reference has no Python release", file=sys.stderr)
            return 1
        print(
            f"Checking running Python {generated['python_release']} behavior against "
            f"recorded Python {reference_release} reference."
        )
        # Keep the original oracle's provenance while comparing every behavior
        # field and the existing canonical fixture formatting. Never write here.
        compared = {**generated, "python_release": reference_release}
        text = json.dumps(compared, ensure_ascii=False, indent=2) + "\n"
        if recorded_text != text:
            print("npm parity fixture differs from the Python reference", file=sys.stderr)
            return 1
    else:
        text = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
