import sys
from types import ModuleType

import pytest

from pollard.estimators.openai import OpenAITokenEstimator


class FakeEncoding:
    def encode(self, value: str) -> list[str]:
        return value.split()


def test_openai_estimator_counts_text_leaves_and_message_overhead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("tiktoken")
    module.encoding_for_model = lambda _model: FakeEncoding()  # type: ignore[attr-defined]
    module.get_encoding = lambda _name: FakeEncoding()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", module)

    estimator = OpenAITokenEstimator(tokens_per_message=3)
    assert estimator.estimate_input_tokens(
        {
            "model": "gpt-fixture",
            "messages": [{"role": "user", "content": "hello world"}],
        }
    ) == 6


def test_openai_estimator_falls_back_for_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("tiktoken")
    module.encoding_for_model = lambda _model: (_ for _ in ()).throw(KeyError())  # type: ignore[attr-defined]
    module.get_encoding = lambda name: FakeEncoding() if name == "cl100k_base" else None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", module)

    assert OpenAITokenEstimator().estimate_input_tokens(
        {"model": "unknown", "input": "one two"}
    ) == 2


def test_openai_estimator_validates_overhead() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        OpenAITokenEstimator(tokens_per_message=-1)
