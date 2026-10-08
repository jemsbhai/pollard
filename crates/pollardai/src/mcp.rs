//! MCP discovery and governed async invocation with a caller-owned session.
//! The session owns the transport, authentication and SDK; discovery never
//! executes tools. JSON inputs/results match Python's MCP bridge contract.
use crate::{ActionSpec, Error, HandlerFuture, Registry, Result, Value};
use std::{collections::BTreeSet, sync::Arc};

pub trait McpSession: Send + Sync {
    fn list_tools(&self) -> HandlerFuture;
    fn call_tool(&self, name: &str, arguments: Value) -> HandlerFuture;
}

pub async fn registry_from_mcp(
    session: Arc<dyn McpSession>,
    exclude: &BTreeSet<String>,
) -> Result<Registry> {
    let listing = session.list_tools().await?;
    registry_from_mcp_listing(&listing, session, exclude)
}

pub fn registry_from_mcp_listing(
    listing: &Value,
    session: Arc<dyn McpSession>,
    exclude: &BTreeSet<String>,
) -> Result<Registry> {
    let empty = Vec::new();
    let tools = match listing.get("tools") {
        None => &empty,
        Some(Value::Array(tools)) => tools,
        _ => {
            return Err(Error::Invalid(
                "MCP tools/list response must contain a tools list".into(),
            ))
        }
    };
    let mut specs = Vec::new();
    for tool in tools {
        let name = tool
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| Error::Invalid("MCP tool missing string field name".into()))?;
        if exclude.contains(name) {
            continue;
        }
        let schema = tool
            .get("inputSchema")
            .or_else(|| tool.get("input_schema"))
            .cloned()
            .unwrap_or_else(|| crate::json!({}));
        if !schema.is_object() {
            return Err(Error::Invalid("MCP tool schema must be an object".into()));
        }
        let description = tool
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("");
        let spec =
            ActionSpec::new(name, "mcp", description, schema, true, None).map_err(|e| match e {
                Error::UnsupportedSchema(detail) => {
                    Error::UnsupportedSchema(format!("MCP tool {name}: {detail}"))
                }
                other => other,
            })?;
        let session = session.clone();
        let name = name.to_owned();
        specs.push(spec.with_async_handler(Arc::new(move |arguments| {
            let future = session.call_tool(&name, arguments);
            Box::pin(async move {
                let result = future.await?;
                Ok(if result.is_object() {
                    result
                } else {
                    crate::json!({"result":result})
                })
            })
        })));
    }
    Registry::new(specs)
}
