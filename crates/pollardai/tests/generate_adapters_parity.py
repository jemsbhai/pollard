"""Run provider-normalization probes against the pinned PyPI 1.6.0 wheel."""
import copy
import hashlib
import json
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
from pollard.adapters import openai, anthropic, bedrock
assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__
source = wheel.parent / "pollard-1.6.0/tests/fixtures"
load = lambda name: json.loads((source / name).read_text())
normalizers = {
    "openai_responses": openai._normalize_response,
    "openai_chat": openai.normalize_chat_completion,
    "litellm": openai.normalize_chat_completion,
    "anthropic": anthropic.normalize_message,
    "bedrock": bedrock.normalize_converse,
}
streamers = {
    "openai_responses": openai._responses_stream,
    "openai_chat": openai._chat_stream,
    "litellm": openai._chat_stream,
    "anthropic": anthropic._messages_stream,
    "bedrock": bedrock._converse_stream,
}
fixtures = {"provenance": {"version": "1.6.0", "wheel_sha256": sha, "generator": Path(__file__).name}, "responses": [], "streams": []}
def response(provider, raw):
    case = {"provider": provider, "raw": copy.deepcopy(raw)}
    try:
        case["expected"] = normalizers[provider](copy.deepcopy(raw))
    except Exception as error:
        case["error"] = str(error)
    fixtures["responses"].append(case)
def stream(provider, raw):
    case = {"provider": provider, "raw": copy.deepcopy(raw), "chunks": []}
    try:
        for chunk in streamers[provider](copy.deepcopy(raw)):
            case["chunks"].append(chunk)
    except Exception as error:
        case["error"] = str(error)
        if hasattr(error, "raw_event"):
            case["raw_error"] = error.raw_event
            case["event_name"] = error.event_name
    fixtures["streams"].append(case)

for provider, filename in [
    ("openai_responses", "openai_response.json"), ("openai_responses", "openai_tool_call.json"),
    ("openai_chat", "openai_chat.json"), ("litellm", "litellm_response.json"),
    ("anthropic", "anthropic_message.json"), ("anthropic", "anthropic_tool_use.json"),
    ("bedrock", "bedrock_converse.json"),
]:
    base = load(filename)
    response(provider, base)
    for usage in [None, {}, [], {"input_tokens": True, "output_tokens": 2}, {"input_tokens": -1, "output_tokens": 2},
                  {"input_tokens": 1.5, "output_tokens": 2}, {"input_tokens": 0, "output_tokens": 0},
                  {"prompt_tokens": 3, "completion_tokens": 2}, {"inputTokens": 3, "outputTokens": 2},
                  {"input_tokens": 2**128, "output_tokens": 2**128},
                  {"input_tokens": 4, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4},
                  {"input_tokens": 4, "output_tokens": 2, "cache_creation_input_tokens": True},
                  {"inputTokens": 2**128, "outputTokens": 1, "cacheReadInputTokens": 2**128, "cacheWriteInputTokens": 7},
                  {"input_tokens": -1, "prompt_tokens": 5, "output_tokens": None, "completion_tokens": 1},
                  {"inputTokens": 1, "outputTokens": 2, "cache_read_input_tokens": False}]:
        response(provider, {**base, "usage": usage})
    response(provider, {k: v for k, v in base.items() if k != "usage"})
    response(provider, {})
response("openai_responses", {"status": "failed", "error": {"message": "generation failed", "code": "server_error"}})
response("openai_responses", {"output_text": "", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ignored"}]}]})
response("anthropic", {"content": [{"type": "text", "text": ""}]})
response("openai_chat", {"choices": [{"message": {"content": "", "tool_calls": []}}]})

for provider, filename in [("openai_chat", "openai_stream.json"), ("litellm", "litellm_stream.json"), ("anthropic", "anthropic_stream.json"), ("bedrock", "bedrock_stream.json")]:
    events = load(filename)
    stream(provider, events)
    stream(provider, [])
    stream(provider, events[:1])
stream("openai_responses", [{"type": "response.output_text.delta", "delta": "hel"}, {"type": "response.output_text.delta", "delta": "lo"}, {"type": "response.completed", "response": load("openai_response.json")}])
stream("openai_responses", [{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.incomplete"}])
stream("openai_responses", [{"type": "response.output_text.delta", "delta": "partial"}])
stream("openai_responses", [{"type": "response.failed", "response": {"error": {"message": "stream failed"}}}])
stream("anthropic", [{"type": "error", "error": {"message": "rate limited", "type": "rate_limit_error"}}])
stream("bedrock", [{"throttlingException": {"message": "slow down"}}])
for index in [0, 2, -1, True, 2**80]:
    stream("openai_chat", [
        {"choices": [{"delta": {"tool_calls": [{"index": index, "id": "id", "type": "function", "function": {"name": "f", "arguments": '{"x":'}}]}}]},
        {"choices": [{"finish_reason": "tool_calls", "delta": {"tool_calls": [{"index": index, "function": {"arguments": "1}"}}]}}]},
    ])
    for argument in ['{"x":1}', '{malformed']:
        stream("anthropic", [
            {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": "id", "name": "f"}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": argument}},
            {"type": "message_stop"},
        ])
        stream("bedrock", [
            {"contentBlockStart": {"contentBlockIndex": index, "start": {"toolUse": {"toolUseId": "id", "name": "f"}}}},
            {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"toolUse": {"input": argument}}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ])
Path(__file__).with_name("pypi160_adapters.json").write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Generated {len(fixtures['responses'])} response and {len(fixtures['streams'])} stream cases")
