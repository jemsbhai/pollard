"""Compare a recording with a separately stored, deterministic live observation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pollard import MemoryStore, ReplayContract, Runtime


def main() -> None:
    store = MemoryStore()
    recorded_contract = ReplayContract(
        provider="example.mock",
        model_revision="snapshot-1",
        application_revision="example-v1",
    )
    payload = recorded_contract.bind(
        {
            "model": "mock-1",
            "messages": [{"role": "user", "content": "Return one stable sentence."}],
        }
    )
    recorded_result = {
        "id": "recorded-response",
        "text": "A stable local response.",
        "usage": {"input_tokens": 8, "output_tokens": 5},
    }
    golden = Runtime(store).run("revalidation-example").model_call(
        payload,
        fn=lambda _payload: recorded_result,
    )
    golden_before = deepcopy(store.get(golden.id))

    def current_provider(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "new-response",
            "text": "A stable local response.",
            "usage": {"input_tokens": 9, "output_tokens": 5},
        }

    report = Runtime(store).run("revalidation-example").revalidate_model_call(
        payload,
        fn=current_provider,
        contract=ReplayContract(
            provider="example.mock",
            model_revision="snapshot-2",
            application_revision="example-v2",
        ),
        observation_id="offline-example",
    )

    assert report.matched
    assert not report.exact_match
    assert store.get(golden.id) == golden_before
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
