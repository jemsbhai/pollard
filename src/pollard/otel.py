"""OpenTelemetry export for Pollard trees."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .store import Store
from .tree import Node, NodeKind


def export_spans(store: Store, root_id: str, tracer: Any) -> int:
    """Export one stored tree as correctly parented OpenTelemetry spans."""

    count = 0

    def visit(node_id: str) -> None:
        nonlocal count
        node = store.get(node_id)
        with tracer.start_as_current_span(
            _span_name(node),
            attributes=span_attributes(node),
        ):
            count += 1
            for child_id in store.children(node.id):
                visit(child_id)

    visit(root_id)
    return count


def live_span_hook(tracer: Any) -> Callable[[Node], None]:
    """Return a Runtime ``on_node`` callback for immediate detached spans.

    Offline ``export_spans`` preserves OpenTelemetry parent contexts. The live
    callback records Pollard's parent id as an attribute because a parent span
    may already have ended when a later child is created.
    """

    def emit(node: Node) -> None:
        attributes = span_attributes(node)
        if node.parent is not None:
            attributes["pollard.parent.id"] = node.parent
        with tracer.start_as_current_span(_span_name(node), attributes=attributes):
            pass

    return emit


def span_attributes(node: Node) -> dict[str, Any]:
    """Return content-free attributes for one node."""

    attributes: dict[str, Any] = {
        "pollard.node.id": node.id,
        "pollard.node.kind": node.kind,
        "pollard.node.attempt": node.attempt,
        "pollard.node.pruned": node.meta.get("pruned") is True,
    }
    if node.result_digest is not None:
        attributes["pollard.result.digest"] = node.result_digest
    registry_digest = node.payload.get("registry_digest") or node.meta.get("registry_digest")
    if isinstance(registry_digest, str):
        attributes["pollard.registry.digest"] = registry_digest
    _add_numeric_attributes(attributes, "pollard.charge", node.meta.get("charges"))
    _add_numeric_attributes(attributes, "pollard.avoided", node.meta.get("avoided"))

    if node.kind == NodeKind.REFUSAL.value:
        reason = node.payload.get("reason")
        if isinstance(reason, str):
            attributes["pollard.refusal.reason"] = reason

    if node.kind == NodeKind.MODEL_CALL.value:
        attributes["gen_ai.operation.name"] = "chat"
        model = node.payload.get("model", node.payload.get("modelId"))
        if isinstance(model, str):
            attributes["gen_ai.request.model"] = model
        provider = _provider(node.payload, model)
        if provider is not None:
            attributes["gen_ai.provider.name"] = provider
        result = node.result
        if isinstance(result, Mapping):
            response_model = result.get("model")
            if isinstance(response_model, str):
                attributes["gen_ai.response.model"] = response_model
        usage = node.meta.get("usage")
        if not isinstance(usage, Mapping) and isinstance(result, Mapping):
            usage = result.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if _is_int(input_tokens):
                attributes["gen_ai.usage.input_tokens"] = input_tokens
            if _is_int(output_tokens):
                attributes["gen_ai.usage.output_tokens"] = output_tokens
    return attributes


def _span_name(node: Node) -> str:
    if node.kind == NodeKind.MODEL_CALL.value:
        model = node.payload.get("model", node.payload.get("modelId", "model"))
        return f"chat {model}"
    if node.kind == NodeKind.TOOL_CALL.value:
        return f"execute_tool {node.payload.get('tool', 'tool')}"
    return f"pollard {node.kind}"


def _provider(payload: Mapping[str, Any], model: object) -> str | None:
    metadata = payload.get("_pollard")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("provider"), str):
        return str(metadata["provider"])
    if not isinstance(model, str):
        return None
    prefixes = {
        "azure/": "azure.ai.openai",
        "bedrock/": "aws.bedrock",
        "vertex_ai/": "gcp.vertex_ai",
        "gemini/": "gcp.gemini",
        "anthropic/": "anthropic",
        "openai/": "openai",
    }
    for prefix, provider in prefixes.items():
        if model.startswith(prefix):
            return provider
    return None


def _add_numeric_attributes(
    attributes: dict[str, Any],
    prefix: str,
    values: object,
) -> None:
    if not isinstance(values, Mapping):
        return
    for name, value in values.items():
        if isinstance(name, str) and _is_number(value):
            attributes[f"{prefix}.{name}"] = value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
