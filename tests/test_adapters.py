import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pollard import AsyncRuntime, MemoryStore, Runtime
from pollard.adapters.anthropic import make_messages_fn
from pollard.adapters.litellm import make_async_completion_fn, make_completion_fn
from pollard.adapters.openai import (
    make_async_chat_completions_fn,
    make_async_responses_fn,
    make_chat_completions_fn,
    make_responses_fn,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FrozenModel:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        return self.value


class SyncCreate:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class AsyncCreate(SyncCreate):
    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class AsyncFrozenStream:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self._values = values

    def __aiter__(self) -> "AsyncFrozenStream":
        self._iterator = iter(self._values)
        return self

    async def __anext__(self) -> FrozenModel:
        try:
            return FrozenModel(next(self._iterator))
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_openai_responses_adapter_normalizes_text_and_usage() -> None:
    endpoint = SyncCreate(FrozenModel(load("openai_response.json")))
    client = SimpleNamespace(responses=endpoint)
    fn = make_responses_fn(client, store=False)

    result = fn({"model": "gpt-5.5", "input": "hello"})

    assert result["text"] == "fixture response"
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert endpoint.calls == [{"store": False, "model": "gpt-5.5", "input": "hello"}]


def test_openai_chat_and_tool_call_fixtures() -> None:
    chat_endpoint = SyncCreate(FrozenModel(load("openai_chat.json")))
    chat_client = SimpleNamespace(chat=SimpleNamespace(completions=chat_endpoint))
    chat = make_chat_completions_fn(chat_client)
    chat_result = chat({"model": "gpt-5.5", "messages": []})
    assert chat_result["text"] == "fixture chat"
    assert chat_result["usage"] == {"input_tokens": 7, "output_tokens": 3}

    tool_endpoint = SyncCreate(FrozenModel(load("openai_tool_call.json")))
    tool_client = SimpleNamespace(chat=SimpleNamespace(completions=tool_endpoint))
    tool_result = make_chat_completions_fn(tool_client)({"model": "gpt-5.5", "messages": []})
    assert tool_result["tool_calls"][0]["function"]["name"] == "weather"
    assert tool_result["usage"] == {"input_tokens": 9, "output_tokens": 5}


def test_openai_stream_fixture_runs_through_pollard() -> None:
    events = [FrozenModel(item) for item in load("openai_stream.json")]
    endpoint = SyncCreate(iter(events))
    client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint))
    seen: list[dict[str, Any]] = []

    node = Runtime(MemoryStore()).run("openai-stream").model_call(
        {"model": "gpt-5.5", "messages": []},
        fn=make_chat_completions_fn(client, stream=True),
        on_delta=seen.append,
        keep_chunks=True,
    )

    assert node.result["text"] == "fixture"
    assert node.result["usage"] == {"input_tokens": 6, "output_tokens": 2}
    assert node.result["chunks"] == seen
    assert endpoint.calls[0]["stream"] is True
    assert endpoint.calls[0]["stream_options"] == {"include_usage": True}


def test_openai_responses_stream_and_tool_output() -> None:
    completed = load("openai_response.json")
    completed["output"].append(
        {
            "type": "function_call",
            "call_id": "call_fixture",
            "name": "weather",
            "arguments": "{\"city\":\"Boston\"}",
        }
    )
    events = [
        FrozenModel({"type": "response.output_text.delta", "delta": "fixture "}),
        FrozenModel({"type": "response.output_text.delta", "delta": "response"}),
        FrozenModel({"type": "response.completed", "response": completed}),
    ]
    endpoint = SyncCreate(iter(events))
    client = SimpleNamespace(responses=endpoint)
    node = Runtime(MemoryStore()).run("responses-stream").model_call(
        {"model": "gpt-5.5", "input": "hello"},
        fn=make_responses_fn(client, stream=True),
    )
    assert node.result["text"] == "fixture response"
    assert node.result["tool_calls"] == [
        {
            "call_id": "call_fixture",
            "name": "weather",
            "arguments": "{\"city\":\"Boston\"}",
        }
    ]


def test_openai_chat_stream_assembles_tool_fragments() -> None:
    events = [
        {
            "id": "chat_tool_stream",
            "model": "gpt-5.5",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fixture",
                                "type": "function",
                                "function": {"name": "wea", "arguments": "{\"city\":"},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "chat_tool_stream",
            "model": "gpt-5.5",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "ther", "arguments": "\"Boston\"}"},
                            }
                        ]
                    },
                }
            ],
        },
    ]
    endpoint = SyncCreate(iter(FrozenModel(event) for event in events))
    client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint))
    node = Runtime(MemoryStore()).run("chat-tool-stream").model_call(
        {"model": "gpt-5.5", "messages": []},
        fn=make_chat_completions_fn(client, stream=True),
    )
    assert node.result["finish_reason"] == "tool_calls"
    assert node.result["tool_calls"][0]["function"] == {
        "name": "weather",
        "arguments": "{\"city\":\"Boston\"}",
    }


def test_anthropic_message_tool_stream_and_count_fixtures() -> None:
    message_endpoint = SyncCreate(FrozenModel(load("anthropic_message.json")))
    message_endpoint.count_tokens = lambda **_kwargs: FrozenModel({"input_tokens": 17})
    client = SimpleNamespace(messages=message_endpoint)
    adapter = make_messages_fn(client, max_tokens=200)
    result = adapter({"model": "claude-sonnet-4-6", "messages": []})
    assert result["text"] == "fixture message"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert adapter.estimate_input_tokens(
        {"model": "claude-sonnet-4-6", "messages": []}
    ) == 17

    tool_endpoint = SyncCreate(FrozenModel(load("anthropic_tool_use.json")))
    tool_endpoint.count_tokens = lambda **_kwargs: 1
    tool_client = SimpleNamespace(messages=tool_endpoint)
    tool_result = make_messages_fn(tool_client)(
        {"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 200}
    )
    assert tool_result["tool_calls"] == [
        {"id": "toolu_weather", "name": "weather", "input": {"city": "Boston"}}
    ]

    stream_endpoint = SyncCreate(iter(FrozenModel(item) for item in load("anthropic_stream.json")))
    stream_endpoint.count_tokens = lambda **_kwargs: FrozenModel({"input_tokens": 13})
    stream_client = SimpleNamespace(messages=stream_endpoint)
    node = Runtime(MemoryStore()).run("anthropic-stream").model_call(
        {"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 200},
        fn=make_messages_fn(stream_client, stream=True),
    )
    assert node.result["text"] == "stream"
    assert node.result["usage"] == {"input_tokens": 13, "output_tokens": 6}
    assert node.result["tool_calls"] == [
        {"id": "toolu_stream", "name": "weather", "input": {"city": "Boston"}}
    ]


def test_litellm_non_stream_and_stream_fixtures() -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(FrozenModel(item) for item in load("litellm_stream.json"))
        return FrozenModel(load("litellm_response.json"))

    result = make_completion_fn(completion)(
        {"model": "anthropic/claude-sonnet-4-6", "messages": []}
    )
    assert result["text"] == "litellm fixture"
    assert result["usage"] == {"input_tokens": 8, "output_tokens": 3}

    node = Runtime(MemoryStore()).run("litellm-stream").model_call(
        {"model": "openai/gpt-5.5", "messages": []},
        fn=make_completion_fn(completion, stream=True),
    )
    assert node.result["text"] == "litellm"
    assert node.result["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert calls[-1]["stream_options"] == {"include_usage": True}


def test_async_openai_and_litellm_stream_factories() -> None:
    async def scenario() -> None:
        endpoint = AsyncCreate(AsyncFrozenStream(load("openai_stream.json")))
        client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint))
        openai_node = await AsyncRuntime(MemoryStore()).run("async-openai-adapter").amodel_call(
            {"model": "gpt-5.5", "messages": []},
            fn=make_async_chat_completions_fn(client, stream=True),
        )
        assert openai_node.result["text"] == "fixture"

        response_events = AsyncFrozenStream(
            [
                {"type": "response.output_text.delta", "delta": "async"},
                {
                    "type": "response.completed",
                    "response": load("openai_response.json"),
                },
            ]
        )
        responses_endpoint = AsyncCreate(response_events)
        responses_client = SimpleNamespace(responses=responses_endpoint)
        responses_node = await AsyncRuntime(MemoryStore()).run(
            "async-responses-adapter"
        ).amodel_call(
            {"model": "gpt-5.5", "input": "hello"},
            fn=make_async_responses_fn(responses_client, stream=True),
        )
        assert responses_node.result["usage"] == {"input_tokens": 11, "output_tokens": 4}

        async def acompletion(**_kwargs: Any) -> AsyncFrozenStream:
            return AsyncFrozenStream(load("litellm_stream.json"))

        litellm_node = await AsyncRuntime(MemoryStore()).run(
            "async-litellm-adapter"
        ).amodel_call(
            {"model": "openai/gpt-5.5", "messages": []},
            fn=make_async_completion_fn(acompletion, stream=True),
        )
        assert litellm_node.result["text"] == "litellm"

    asyncio.run(scenario())
