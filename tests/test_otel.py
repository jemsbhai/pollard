import pytest

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime
from pollard.otel import export_spans, live_span_hook

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _tracer() -> tuple[object, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("pollard-test"), exporter


def test_offline_span_tree_matches_branched_refused_replayed_tree() -> None:
    store = MemoryStore()
    run = Runtime(store).run("otel-tree")
    payload = {
        "_pollard": {"provider": "aws.bedrock"},
        "modelId": "us.amazon.nova-lite-v1:0",
    }
    model = run.model_call(
        payload,
        fn=lambda _payload: {
            "text": "ok",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    run.rollback(run.root_id)
    with (
        run.branch(attempt=1, budget=Budget(steps=0)) as branch,
        pytest.raises(BudgetExceeded),
    ):
        branch.model_call({"model": "blocked"}, fn=lambda _payload: {"text": "no"})
    replay = Runtime(store, mode="hybrid").run("otel-tree")
    replay.model_call(payload, fn=lambda _payload: {"text": "not called"})

    tracer, exporter = _tracer()
    count = export_spans(store, run.root_id, tracer)
    spans = exporter.get_finished_spans()

    assert count == len(list(store.walk(run.root_id))) == len(spans)
    by_node = {span.attributes["pollard.node.id"]: span for span in spans}
    for node in store.walk(run.root_id):
        span = by_node[node.id]
        if node.parent is None:
            assert span.parent is None
        else:
            assert span.parent is not None
            assert span.parent.span_id == by_node[node.parent].context.span_id
    model_span = by_node[model.id]
    assert model_span.attributes["gen_ai.provider.name"] == "aws.bedrock"
    assert model_span.attributes["gen_ai.usage.input_tokens"] == 2
    assert model_span.attributes["pollard.avoided.steps"] == 1
    refusal = next(node for node in store.walk(run.root_id) if node.kind == "refusal")
    assert by_node[refusal.id].attributes["pollard.refusal.reason"] == "budget"


def test_live_span_hook_emits_new_nodes_without_payload_content() -> None:
    tracer, exporter = _tracer()
    run = Runtime(MemoryStore(), on_node=live_span_hook(tracer)).run("live-otel")
    node = run.model_call(
        {"model": "openai/test", "prompt": "do not export me"},
        fn=lambda _payload: {
            "text": "also private",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    attributes = next(
        span.attributes for span in spans if span.attributes["pollard.node.id"] == node.id
    )
    assert "do not export me" not in str(attributes)
    assert "also private" not in str(attributes)
