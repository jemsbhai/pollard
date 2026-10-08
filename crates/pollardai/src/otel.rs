//! Content-free OpenTelemetry attributes and correctly parented export.
//! Implement `SpanExporter` for a caller-owned tracing SDK. This module never
//! configures global tracing, creates a network exporter or reads credentials.
use crate::{Error, Node, NodeKind, Result, Store, Value};
use std::collections::{BTreeMap, BTreeSet};

pub type SpanAttributes = BTreeMap<String, Value>;

pub fn span_attributes(node: &Node) -> SpanAttributes {
    let mut attributes = BTreeMap::from([
        ("pollard.node.id".into(), crate::json!(node.id)),
        ("pollard.node.kind".into(), crate::json!(node.kind)),
        ("pollard.node.attempt".into(), crate::json!(node.attempt)),
        (
            "pollard.node.pruned".into(),
            crate::json!(node.meta.get("pruned") == Some(&Value::Bool(true))),
        ),
    ]);
    if let Some(digest) = &node.result_digest {
        attributes.insert("pollard.result.digest".into(), crate::json!(digest));
    }
    let registry = node
        .payload
        .get("registry_digest")
        .filter(|v| truthy(v))
        .or_else(|| node.meta.get("registry_digest"));
    if let Some(Value::String(digest)) = registry {
        attributes.insert("pollard.registry.digest".into(), crate::json!(digest));
    }
    for (key, prefix) in [
        ("charges", "pollard.charge"),
        ("avoided", "pollard.avoided"),
    ] {
        if let Some(values) = node.meta.get(key).and_then(Value::as_object) {
            for (name, value) in values {
                if value.is_number() {
                    attributes.insert(format!("{prefix}.{name}"), value.clone());
                }
            }
        }
    }
    if node.kind == NodeKind::Refusal {
        if let Some(Value::String(reason)) = node.payload.get("reason") {
            attributes.insert("pollard.refusal.reason".into(), crate::json!(reason));
        }
    }
    if node.kind == NodeKind::ModelCall {
        attributes.insert("gen_ai.operation.name".into(), crate::json!("chat"));
        let model = node
            .payload
            .get("model")
            .or_else(|| node.payload.get("modelId"));
        if let Some(Value::String(model)) = model {
            attributes.insert("gen_ai.request.model".into(), crate::json!(model));
        }
        let explicit = node
            .payload
            .get("_pollard")
            .and_then(|v| v.get("provider"))
            .and_then(Value::as_str);
        let provider = explicit.or_else(|| {
            model.and_then(Value::as_str).and_then(|model| {
                [
                    ("azure/", "azure.ai.openai"),
                    ("bedrock/", "aws.bedrock"),
                    ("vertex_ai/", "gcp.vertex_ai"),
                    ("gemini/", "gcp.gemini"),
                    ("anthropic/", "anthropic"),
                    ("openai/", "openai"),
                ]
                .into_iter()
                .find_map(|(prefix, name)| model.starts_with(prefix).then_some(name))
            })
        });
        if let Some(provider) = provider {
            attributes.insert("gen_ai.provider.name".into(), crate::json!(provider));
        }
        let result = node.result.as_ref();
        if let Some(Value::String(model)) = result.and_then(|v| v.get("model")) {
            attributes.insert("gen_ai.response.model".into(), crate::json!(model));
        }
        let usage = node
            .meta
            .get("usage")
            .filter(|v| v.is_object())
            .or_else(|| result.and_then(|v| v.get("usage")))
            .and_then(Value::as_object);
        if let Some(usage) = usage {
            for key in ["input_tokens", "output_tokens"] {
                if let Some(value) = usage
                    .get(key)
                    .filter(|v| crate::identity::integer_text(v).is_some())
                {
                    attributes.insert(format!("gen_ai.usage.{key}"), value.clone());
                }
            }
        }
    }
    attributes
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
pub fn span_name(node: &Node) -> String {
    match node.kind {
        NodeKind::ModelCall => format!(
            "chat {}",
            display(
                node.payload
                    .get("model")
                    .or_else(|| node.payload.get("modelId")),
                "model"
            )
        ),
        NodeKind::ToolCall => format!("execute_tool {}", display(node.payload.get("tool"), "tool")),
        _ => format!("pollard {}", node.kind.as_str()),
    }
}
fn display(value: Option<&Value>, fallback: &str) -> String {
    match value {
        None => fallback.into(),
        Some(value) => crate::python_display::display(value),
    }
}
pub trait SpanExporter {
    type Span;
    fn start_span(
        &mut self,
        name: &str,
        attributes: &SpanAttributes,
        parent: Option<&Self::Span>,
    ) -> Result<Self::Span>;
    fn end_span(&mut self, span: Self::Span, failed: bool) -> Result<()>;
}
struct Active<'a, T: SpanExporter> {
    exporter: &'a mut T,
    spans: Vec<T::Span>,
}
impl<T: SpanExporter> Drop for Active<'_, T> {
    fn drop(&mut self) {
        while let Some(span) = self.spans.pop() {
            let _ = self.exporter.end_span(span, true);
        }
    }
}
/// Export a stored tree with explicit parents; close every active span on error.
pub fn export_spans<S: Store + ?Sized, T: SpanExporter>(
    store: &S,
    root: &str,
    exporter: &mut T,
) -> Result<usize> {
    let mut active = Active {
        exporter,
        spans: Vec::new(),
    };
    let mut pending = vec![Some(root.to_owned())];
    let mut seen = BTreeSet::new();
    let mut count = 0;
    while let Some(id) = pending.pop() {
        if let Some(id) = id {
            if !seen.insert(id.clone()) {
                return Err(Error::Integrity("cycle or duplicate in span tree".into()));
            }
            let node = store.get(&id)?;
            node.validate()?;
            let span = active.exporter.start_span(
                &span_name(&node),
                &span_attributes(&node),
                active.spans.last(),
            )?;
            active.spans.push(span);
            count += 1;
            pending.push(None);
            pending.extend(store.children(&id)?.into_iter().rev().map(Some));
        } else {
            let span = active.spans.pop().expect("balanced traversal");
            active.exporter.end_span(span, false)?;
        }
    }
    Ok(count)
}
/// Emit a detached completed live span; Pollard parent identity is an attribute.
pub fn export_live_span<T: SpanExporter>(node: &Node, exporter: &mut T) -> Result<()> {
    let mut attributes = span_attributes(node);
    if let Some(parent) = &node.parent {
        attributes.insert("pollard.parent.id".into(), crate::json!(parent));
    }
    let span = exporter.start_span(&span_name(node), &attributes, None)?;
    exporter.end_span(span, false)
}
