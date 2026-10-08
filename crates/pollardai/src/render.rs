//! Offline, content-minimal tree views. Payload/result content is opt-in.
use crate::{contains_redaction, Error, Node, NodeKind, Result, Store, Value};
use std::collections::BTreeSet;

fn display(value: &Value) -> String {
    crate::python_display::display(value)
}
pub fn label(node: &Node) -> String {
    let string = |key: &str, fallback: &str| {
        node.payload
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or(fallback)
            .to_owned()
    };
    match node.kind {
        NodeKind::Root => string("run", "run"),
        NodeKind::ModelCall => node
            .payload
            .get("model")
            .or_else(|| node.payload.get("modelId"))
            .and_then(Value::as_str)
            .unwrap_or("model")
            .to_owned(),
        NodeKind::ToolCall => string("tool", "tool"),
        NodeKind::Refusal => {
            let reason = node
                .payload
                .get("reason")
                .map(display)
                .unwrap_or_else(|| "refusal".into());
            node.payload
                .get("meter")
                .and_then(Value::as_str)
                .map_or_else(|| reason.clone(), |m| format!("{reason}:{m}"))
        }
        NodeKind::Note => {
            if node.payload.get("branch") == Some(&Value::Bool(true)) {
                return "branch".into();
            }
            ["label", "checkpoint", "status"]
                .into_iter()
                .find_map(|key| {
                    node.payload
                        .get(key)
                        .filter(|v| v.is_string() || v.is_number())
                        .map(|v| format!("{key}={}", display(v)))
                })
                .unwrap_or_else(|| "note".into())
        }
    }
}
/// Machine-readable tree view with the PyPI 1.6.0 CLI field contract.
/// Payloads and results are included only when explicitly requested.
pub fn tree_document<S: Store + ?Sized>(
    store: &S,
    root: &str,
    include_payloads: bool,
) -> Result<Value> {
    let numeric_mapping = |value: Option<&Value>| -> Value {
        Value::Object(
            value
                .and_then(Value::as_object)
                .into_iter()
                .flatten()
                .filter(|(_, amount)| amount.is_number())
                .map(|(key, amount)| (key.clone(), amount.clone()))
                .collect(),
        )
    };
    let mut nodes = Vec::new();
    for node in store.walk(root)? {
        node.validate()?;
        let mut item = crate::json!({
            "id": node.id,
            "parent": node.parent,
            "kind": node.kind,
            "attempt": node.attempt,
            "label": label(&node),
            "charges": numeric_mapping(node.meta.get("charges")),
            "avoided": numeric_mapping(node.meta.get("avoided")),
            "refusal": node.kind == NodeKind::Refusal,
            "pruned": node.meta.get("pruned") == Some(&Value::Bool(true)),
            "redacted": contains_redaction(&node.payload),
            "children": store.children(&node.id)?,
        });
        if include_payloads {
            item["payload"] = node.payload;
            item["result"] = node.result.unwrap_or(Value::Null);
        }
        nodes.push(item);
    }
    Ok(crate::json!({"root_id": root, "nodes": nodes}))
}
fn markers(node: &Node) -> Vec<&'static str> {
    let mut markers = Vec::new();
    if node.kind == NodeKind::Refusal {
        markers.push("REFUSED");
    }
    if node.meta.get("pruned") == Some(&Value::Bool(true)) {
        markers.push("PRUNED");
    }
    if contains_redaction(&node.payload) {
        markers.push("REDACTED");
    }
    markers
}
fn charges(node: &Node) -> String {
    let mut pairs = node
        .meta
        .get("charges")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .filter(|(_, v)| v.is_number())
        .collect::<Vec<_>>();
    pairs.sort_by_key(|(name, _)| *name);
    pairs
        .into_iter()
        .map(|(name, amount)| format!("{name}={}", display(amount)))
        .collect::<Vec<_>>()
        .join(" ")
}
pub fn render_ascii<S: Store + ?Sized>(
    store: &S,
    root: &str,
    unicode: bool,
    include_payloads: bool,
) -> Result<String> {
    let (tee, elbow, pipe, blank) = if unicode {
        ("├─ ", "└─ ", "│  ", "   ")
    } else {
        ("|-- ", "\\-- ", "|   ", "    ")
    };
    let mut pending = vec![(root.to_owned(), String::new(), true, true)];
    let mut seen = BTreeSet::new();
    let mut lines = Vec::new();
    while let Some((id, prefix, last, is_root)) = pending.pop() {
        if !seen.insert(id.clone()) {
            return Err(Error::Integrity(
                "cycle or duplicate in rendered tree".into(),
            ));
        }
        let node = store.get(&id)?;
        node.validate()?;
        let connector = if is_root {
            ""
        } else if last {
            elbow
        } else {
            tee
        };
        let charges = charges(&node);
        let charges = if charges.is_empty() {
            String::new()
        } else {
            format!(" charges[{charges}]")
        };
        let mut marker_text = String::new();
        for marker in markers(&node) {
            marker_text.push_str(" [");
            marker_text.push_str(marker);
            marker_text.push(']');
        }
        lines.push(format!(
            "{prefix}{connector}{} {} {}{charges}{marker_text}",
            node.kind.as_str(),
            &node.id[..8],
            label(&node)
        ));
        let body_prefix = format!(
            "{prefix}{}",
            if is_root {
                ""
            } else if last {
                blank
            } else {
                pipe
            }
        );
        if include_payloads {
            lines.push(format!(
                "{body_prefix}    payload={}",
                crate::result_text_and_digest(&node.payload)?.0
            ));
            if let Some(result) = node.result {
                lines.push(format!(
                    "{body_prefix}    result={}",
                    crate::result_text_and_digest(&result)?.0
                ));
            }
        }
        let children = store.children(&id)?;
        let count = children.len();
        for (index, child) in children.into_iter().enumerate().rev() {
            pending.push((child, body_prefix.clone(), index + 1 == count, false));
        }
    }
    Ok(lines.join("\n"))
}
fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}
pub fn render_html<S: Store + ?Sized>(
    store: &S,
    root: &str,
    include_payloads: bool,
) -> Result<String> {
    let root_node = store.get(root)?;
    let title = escape(&format!("Pollard run: {}", label(&root_node)));
    let mut html=format!("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{title}</title><style>\n:root{{color-scheme:light dark;font-family:ui-monospace,Consolas,monospace}}body{{margin:2rem;max-width:100rem}}h1{{font:600 1.3rem system-ui,sans-serif}}ul{{list-style:none;margin:0 0 0 1rem;padding-left:1rem;border-left:1px solid #8886}}li{{margin:.35rem 0}}summary{{cursor:pointer}}.id{{color:#777}}.charges{{color:#087f5b}}.refusal>details>summary{{color:#c92a2a;font-weight:700}}.pruned{{opacity:.5}}.redacted>details>summary{{text-decoration:underline dotted}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:.6rem;background:#8881}}\n</style></head><body><h1>{title}</h1><ul>");
    let mut pending = vec![Some(root.to_owned())];
    let mut seen = BTreeSet::new();
    while let Some(id) = pending.pop() {
        let Some(id) = id else {
            html.push_str("</ul></details></li>");
            continue;
        };
        if !seen.insert(id.clone()) {
            return Err(Error::Integrity(
                "cycle or duplicate in rendered tree".into(),
            ));
        }
        let node = store.get(&id)?;
        node.validate()?;
        let markers = markers(&node);
        let classes = markers
            .iter()
            .map(|s| match *s {
                "REFUSED" => "refusal",
                "PRUNED" => "pruned",
                _ => "redacted",
            })
            .collect::<Vec<_>>()
            .join(" ");
        let charges = charges(&node);
        let marks = if markers.is_empty() {
            String::new()
        } else {
            format!(" [{}]", markers.join(", "))
        };
        html.push_str(&format!("<li class=\"{classes}\"><details open><summary>{} <span class=\"id\">{}</span> {} <span class=\"charges\">{}</span>{marks}</summary>",node.kind.as_str(),&node.id[..8],escape(&label(&node)),escape(&charges)));
        if include_payloads {
            let payload = serde_json::to_string_pretty(&node.payload)
                .map_err(|e| Error::Invalid(e.to_string()))?;
            let result = serde_json::to_string_pretty(&node.result)
                .map_err(|e| Error::Invalid(e.to_string()))?;
            html.push_str(&format!("<details><summary>payload and result</summary><pre>payload={}\nresult={}</pre></details>",escape(&payload),escape(&result)));
        }
        html.push_str("<ul>");
        pending.push(None);
        pending.extend(store.children(&id)?.into_iter().rev().map(Some));
    }
    html.push_str("</ul></body></html>\n");
    Ok(html)
}
