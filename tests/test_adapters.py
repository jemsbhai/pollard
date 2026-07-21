import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pollard import AsyncRuntime, MemoryStore, Runtime
from pollard.adapters.anthropic import AnthropicStreamError, make_messages_fn
from pollard.adapters.bedrock import BedrockStreamError, make_converse_fn
from pollard.adapters.litellm import make_async_completion_fn, make_completion_fn
from pollard.adapters.openai import (
    OpenAIResponseError,
    make_async_chat_completions_fn,
    make_async_responses_fn,
    make_chat_completions_fn,
    make_responses_fn,
)
from pollard.errors import is_post_dispatch_outcome_unknown

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
    assert result["provider_usage"] == load("openai_response.json")["usage"]
    assert endpoint.calls == [{"store": False, "model": "gpt-5.5", "input": "hello"}]


def test_openai_responses_adapter_surfaces_failed_response() -> None:
    raw_response = {
        "id": "resp_failed_nonstream",
        "model": "gpt-5.5",
        "status": "failed",
        "error": {"code": "server_error", "message": "non-stream generation failed"},
        "output": [],
        "usage": None,
    }
    endpoint = SyncCreate(FrozenModel(raw_response))
    adapter = make_responses_fn(SimpleNamespace(responses=endpoint))

    with pytest.raises(OpenAIResponseError, match="non-stream generation failed") as raised:
        adapter({"model": "gpt-5.5", "input": "hello"})

    assert raised.value.event_name == "response.failed"
    assert raised.value.raw_event == {
        "type": "response.failed",
        "response": raw_response,
    }
    assert is_post_dispatch_outcome_unknown(raised.value)


def test_adapter_retains_but_does_not_normalize_invalid_provider_usage() -> None:
    raw_response = load("openai_response.json")
    raw_response["usage"] = {"input_tokens": -1, "output_tokens": 4}
    endpoint = SyncCreate(FrozenModel(raw_response))

    result = make_responses_fn(SimpleNamespace(responses=endpoint))(
        {"model": "gpt-5.5", "input": "hello"}
    )

    assert "usage" not in result
    assert result["provider_usage"] == {"input_tokens": -1, "output_tokens": 4}


def test_adapter_preserves_native_error_and_marks_unknown_outcome() -> None:
    transport_error = OSError("transport detail fixture")
    provider_error = RuntimeError("provider request id fixture")

    class Endpoint:
        def create(self, **_kwargs: Any) -> Any:
            raise provider_error from transport_error

    adapter = make_responses_fn(SimpleNamespace(responses=Endpoint()))
    with pytest.raises(RuntimeError) as direct_error:
        adapter({"model": "gpt-5.5", "input": "hello"})
    assert direct_error.value is provider_error
    assert is_post_dispatch_outcome_unknown(provider_error)

    run = Runtime(MemoryStore()).run("adapter-error")
    with pytest.raises(RuntimeError) as runtime_error:
        run.model_call(
            {"model": "gpt-5.5", "input": "private prompt"},
            fn=adapter,
        )
    assert runtime_error.value is provider_error
    assert runtime_error.value.__cause__ is transport_error
    assert run.cursor.payload["event"] == "call_outcome_unknown"
    assert run.cursor.meta["failure"]["error_type"] == "RuntimeError"
    assert "provider request id fixture" not in str(run.cursor)
    assert "private prompt" not in str(run.cursor)


def test_adapter_marks_operator_interrupt_after_dispatch() -> None:
    provider_error = KeyboardInterrupt("operator interrupted dispatched request")

    class Endpoint:
        def create(self, **_kwargs: Any) -> Any:
            raise provider_error

    adapter = make_responses_fn(SimpleNamespace(responses=Endpoint()))
    with pytest.raises(KeyboardInterrupt) as raised:
        adapter({"model": "gpt-5.5", "input": "hello"})

    assert raised.value is provider_error
    assert is_post_dispatch_outcome_unknown(provider_error)


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


def test_openai_responses_stream_preserves_incomplete_usage() -> None:
    event = FrozenModel(
        {
            "type": "response.incomplete",
            "response": {
                "id": "resp_incomplete",
                "model": "gpt-5.5",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        }
    )
    endpoint = SyncCreate(iter([event]))
    stream = make_responses_fn(SimpleNamespace(responses=endpoint), stream=True)(
        {"model": "gpt-5.5", "input": "hello"}
    )

    chunks = list(stream)

    assert chunks[-1]["result"]["status"] == "incomplete"
    assert chunks[-1]["result"]["text"] == "partial"
    assert chunks[-1]["result"]["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_openai_responses_stream_surfaces_failed_event_details() -> None:
    raw_event = {
        "type": "response.failed",
        "response": {
            "id": "resp_failed",
            "model": "gpt-5.5",
            "status": "failed",
            "error": {"code": "server_error", "message": "generation failed"},
            "output": [],
            "usage": None,
        },
    }
    endpoint = SyncCreate(iter([FrozenModel(raw_event)]))
    stream = make_responses_fn(SimpleNamespace(responses=endpoint), stream=True)(
        {"model": "gpt-5.5", "input": "hello"}
    )

    with pytest.raises(OpenAIResponseError, match="generation failed") as raised:
        list(stream)

    assert raised.value.event_name == "response.failed"
    assert raised.value.raw_event == raw_event
    assert raised.value.response_id == "resp_failed"
    assert raised.value.code == "server_error"
    assert is_post_dispatch_outcome_unknown(raised.value)


def test_openai_responses_stream_requires_terminal_event() -> None:
    events = [FrozenModel({"type": "response.output_text.delta", "delta": "partial"})]
    endpoint = SyncCreate(iter(events))
    stream = make_responses_fn(
        SimpleNamespace(responses=endpoint),
        stream=True,
    )({"model": "gpt-5.5", "input": "hello"})

    with pytest.raises(OpenAIResponseError, match="without a terminal event") as raised:
        list(stream)

    assert raised.value.event_name == "response.stream_ended"
    assert is_post_dispatch_outcome_unknown(raised.value)


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
    count_calls: list[dict[str, Any]] = []

    def count_tokens(**kwargs: Any) -> FrozenModel:
        count_calls.append(kwargs)
        return FrozenModel({"input_tokens": 17})

    message_endpoint.count_tokens = count_tokens
    client = SimpleNamespace(messages=message_endpoint)
    adapter = make_messages_fn(client, max_tokens=200)
    result = adapter({"model": "claude-sonnet-4-6", "messages": []})
    assert result["text"] == "fixture message"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert result["provider_usage"] == load("anthropic_message.json")["usage"]
    assert adapter.estimate_input_tokens(
        {"model": "claude-sonnet-4-6", "messages": []}
    ) == 17
    assert count_calls == [{"model": "claude-sonnet-4-6", "messages": []}]

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


def test_anthropic_usage_includes_cached_input_tokens() -> None:
    response = {
        "content": [],
        "usage": {
            "input_tokens": 5,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 11,
            "output_tokens": 3,
        },
    }
    endpoint = SyncCreate(FrozenModel(response))
    result = make_messages_fn(SimpleNamespace(messages=endpoint))(
        {"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 32}
    )

    assert result["usage"] == {"input_tokens": 23, "output_tokens": 3}
    assert result["provider_usage"] == response["usage"]


def test_anthropic_stream_surfaces_error_event_details() -> None:
    raw_event = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "fixture overloaded"},
        "request_id": "req_fixture",
    }
    endpoint = SyncCreate(iter([FrozenModel(raw_event)]))
    stream = make_messages_fn(
        SimpleNamespace(messages=endpoint),
        stream=True,
    )({"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 32})

    with pytest.raises(AnthropicStreamError, match="fixture overloaded") as raised:
        list(stream)

    assert raised.value.event_name == "error"
    assert raised.value.error_type == "overloaded_error"
    assert raised.value.request_id == "req_fixture"
    assert raised.value.raw_event == raw_event
    assert is_post_dispatch_outcome_unknown(raised.value)


def test_anthropic_stream_requires_message_stop() -> None:
    endpoint = SyncCreate(
        iter(
            [
                FrozenModel(
                    {
                        "type": "message_start",
                        "message": {"usage": {"input_tokens": 2}},
                    }
                )
            ]
        )
    )
    stream = make_messages_fn(
        SimpleNamespace(messages=endpoint),
        stream=True,
    )({"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 32})

    with pytest.raises(AnthropicStreamError, match="without message_stop") as raised:
        list(stream)

    assert raised.value.event_name == "stream_ended"
    assert is_post_dispatch_outcome_unknown(raised.value)


def test_anthropic_count_tokens_uses_a_positive_request_projection() -> None:
    endpoint = SyncCreate(FrozenModel(load("anthropic_message.json")))
    count_calls: list[dict[str, Any]] = []

    def count_tokens(**kwargs: Any) -> FrozenModel:
        count_calls.append(kwargs)
        return FrozenModel({"input_tokens": 23})

    endpoint.count_tokens = count_tokens
    adapter = make_messages_fn(
        SimpleNamespace(messages=endpoint),
        max_tokens=128,
        metadata={"user_id": "fixture"},
        temperature=0.2,
    )
    payload = {
        "_pollard": {"provider": "anthropic"},
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hello"}],
        "system": "system",
        "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
        "cache_control": {"type": "ephemeral"},
        "service_tier": "auto",
        "future_generation_only": True,
    }

    assert adapter.estimate_input_tokens(payload) == 23
    assert count_calls == [
        {
            "model": payload["model"],
            "messages": payload["messages"],
            "system": "system",
            "tools": payload["tools"],
            "tool_choice": payload["tool_choice"],
            "thinking": payload["thinking"],
            "output_config": payload["output_config"],
            "cache_control": payload["cache_control"],
        }
    ]

    adapter(payload)
    assert endpoint.calls[0]["max_tokens"] == 128
    assert endpoint.calls[0]["metadata"] == {"user_id": "fixture"}
    assert endpoint.calls[0]["future_generation_only"] is True
    assert "_pollard" not in endpoint.calls[0]


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


def test_bedrock_converse_normalizes_tools_usage_and_count_tokens() -> None:
    calls: list[dict[str, Any]] = []
    count_calls: list[dict[str, Any]] = []

    class Client:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return load("bedrock_converse.json")

        def count_tokens(self, **kwargs: Any) -> dict[str, int]:
            count_calls.append(kwargs)
            return {"inputTokens": 21}

    adapter = make_converse_fn(Client(), count_tokens=True, guardrailConfig={"trace": "enabled"})
    payload = {
        "modelId": "us.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"text": "hello"}]}],
        "inferenceConfig": {"maxTokens": 64},
    }
    result = adapter(payload)

    assert isinstance(result, dict)
    assert result["text"] == "fixture bedrock"
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 6}
    assert result["provider_usage"] == load("bedrock_converse.json")["usage"]
    assert result["tool_calls"] == [
        {
            "toolUseId": "tooluse_weather",
            "name": "weather",
            "input": {"city": "Boston"},
        }
    ]
    assert calls[0]["guardrailConfig"] == {"trace": "enabled"}
    assert adapter.estimate_input_tokens(payload) == 21
    assert count_calls == [
        {
            "modelId": "us.amazon.nova-lite-v1:0",
            "input": {
                "converse": {
                    "messages": payload["messages"],
                }
            },
        }
    ]


def test_bedrock_usage_includes_cached_input_tokens() -> None:
    response = load("bedrock_converse.json")
    response["usage"] = {
        "inputTokens": 5,
        "cacheReadInputTokens": 7,
        "cacheWriteInputTokens": 11,
        "outputTokens": 3,
    }

    class Client:
        def converse(self, **_kwargs: Any) -> dict[str, Any]:
            return response

    result = make_converse_fn(Client())(
        {"modelId": "us.amazon.nova-lite-v1:0", "messages": []}
    )

    assert result["usage"] == {"input_tokens": 23, "output_tokens": 3}
    assert result["provider_usage"] == response["usage"]


def test_bedrock_converse_stream_runs_through_pollard() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"stream": iter(load("bedrock_stream.json"))}

    seen: list[dict[str, Any]] = []
    node = Runtime(MemoryStore()).run("bedrock-stream").model_call(
        {
            "_pollard": {"provider": "aws.bedrock"},
            "modelId": "us.amazon.nova-lite-v1:0",
            "messages": [],
        },
        fn=make_converse_fn(Client(), stream=True),
        on_delta=seen.append,
        keep_chunks=True,
    )

    assert node.result["text"] == "bedrock"
    assert node.result["usage"] == {"input_tokens": 9, "output_tokens": 5}
    assert node.result["tool_calls"] == [
        {
            "toolUseId": "tooluse_stream",
            "name": "weather",
            "input": {"city": "Boston"},
        }
    ]
    assert node.result["chunks"] == seen
    assert calls[0]["modelId"] == "us.amazon.nova-lite-v1:0"
    assert "_pollard" not in calls[0]


def test_bedrock_stream_surfaces_provider_errors() -> None:
    raw_event = {
        "throttlingException": {"message": "fixture throttle", "retryable": True}
    }

    class Client:
        def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
            return {"stream": iter([raw_event])}

    stream = make_converse_fn(Client(), stream=True)({"modelId": "fixture", "messages": []})
    with pytest.raises(
        BedrockStreamError,
        match=r"throttlingException.*fixture throttle",
    ) as error:
        list(stream)
    assert error.value.event_name == "throttlingException"
    assert error.value.raw_event == raw_event
    assert is_post_dispatch_outcome_unknown(error.value)


def test_bedrock_stream_requires_message_stop() -> None:
    class Client:
        def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
            return {"stream": iter([{"messageStart": {"role": "assistant"}}])}

    stream = make_converse_fn(Client(), stream=True)(
        {"modelId": "fixture", "messages": []}
    )
    with pytest.raises(BedrockStreamError, match="streamEnded") as raised:
        list(stream)
    assert raised.value.event_name == "streamEnded"
    assert is_post_dispatch_outcome_unknown(raised.value)


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
