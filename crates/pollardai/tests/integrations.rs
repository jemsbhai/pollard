use pollardai::*;
use pollardai::{mcp::*, otel::*};
use std::{
    collections::BTreeSet,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};
fn fixtures() -> Value {
    serde_json::from_str(include_str!("pypi160_integrations.json")).unwrap()
}
struct Session {
    listing: Value,
    calls: Arc<AtomicUsize>,
}
impl McpSession for Session {
    fn list_tools(&self) -> HandlerFuture {
        let listing = self.listing.clone();
        Box::pin(async move { Ok(listing) })
    }
    fn call_tool(&self, name: &str, arguments: Value) -> HandlerFuture {
        assert_eq!(name, "echo");
        self.calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move { Ok(json!({"content":[{"type":"text","text":arguments["text"]}]})) })
    }
}
#[test]
fn mcp_discovery_matches_python_registry_digests_and_failures() {
    for case in fixtures()["mcp"].as_array().unwrap() {
        let calls = Arc::new(AtomicUsize::new(0));
        let session = Arc::new(Session {
            listing: case["listing"].clone(),
            calls: calls.clone(),
        });
        let exclude: BTreeSet<String> = serde_json::from_value(case["exclude"].clone()).unwrap();
        let result = futures::executor::block_on(registry_from_mcp(session, &exclude));
        if case.get("error").is_some() {
            assert!(result.is_err(), "{case}");
        } else {
            assert_eq!(
                result.unwrap().registry_digest(),
                case["digest"].as_str().unwrap()
            );
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
}
#[test]
fn governed_mcp_dispatch_redacts_identity_and_replays_without_transport() {
    let calls = Arc::new(AtomicUsize::new(0));
    let session = Arc::new(Session {
        listing: fixtures()["mcp"][2]["listing"].clone(),
        calls: calls.clone(),
    });
    let registry =
        futures::executor::block_on(registry_from_mcp(session, &BTreeSet::new())).unwrap();
    let runtime = Runtime::memory(ReplayMode::Record).with_registry(registry.clone());
    let mut run = runtime
        .run(
            "mcp",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let node = futures::executor::block_on(run.registered_tool_call_async(
        "echo",
        None,
        json!({"text":"private"}),
        CallOptions::default(),
    ))
    .unwrap();
    assert!(contains_redaction(&node.payload));
    assert!(!node.payload.to_string().contains("private"));
    assert_eq!(
        node.result.as_ref().unwrap()["content"][0]["text"],
        "private"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let replay =
        Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay).with_registry(registry);
    let mut run = replay.run("mcp", None, 0).unwrap();
    assert_eq!(
        futures::executor::block_on(run.registered_tool_call_async(
            "echo",
            None,
            json!({"text":"private"}),
            CallOptions::default()
        ))
        .unwrap()
        .id,
        node.id
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[test]
fn telemetry_attributes_match_python_without_payload_or_result_content() {
    for case in fixtures()["otel"].as_array().unwrap() {
        let node: Node = serde_json::from_value(case["node"].clone()).unwrap();
        let actual = serde_json::to_value(span_attributes(&node)).unwrap();
        assert_eq!(actual, case["attributes"], "{node:?}");
        assert_eq!(span_name(&node), case["name"].as_str().unwrap());
        assert!(!actual.to_string().contains("PRIVATE_"));
    }
}
#[derive(Default)]
struct Exporter {
    events: Vec<String>,
    starts: usize,
    fail_at: Option<usize>,
}
impl SpanExporter for Exporter {
    type Span = usize;
    fn start_span(
        &mut self,
        _: &str,
        attributes: &SpanAttributes,
        parent: Option<&usize>,
    ) -> Result<usize> {
        self.starts += 1;
        if self.fail_at == Some(self.starts) {
            return Err(Error::Handler("exporter unavailable".into()));
        }
        assert!(!attributes.contains_key("payload"));
        self.events
            .push(format!("start:{}:{parent:?}", self.starts));
        Ok(self.starts)
    }
    fn end_span(&mut self, span: usize, failed: bool) -> Result<()> {
        self.events.push(format!("end:{span}:{failed}"));
        Ok(())
    }
}
#[test]
fn telemetry_parents_and_cleanup_match_tree_even_on_export_failure() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("spans", None, 0).unwrap();
    let root = run.root_id().to_owned();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    run.tool_call("echo", json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    let mut exporter = Exporter::default();
    assert_eq!(
        export_spans(&*runtime.store(), &root, &mut exporter).unwrap(),
        3
    );
    assert_eq!(
        exporter.events,
        vec![
            "start:1:None",
            "start:2:Some(1)",
            "start:3:Some(2)",
            "end:3:false",
            "end:2:false",
            "end:1:false"
        ]
    );
    let mut exporter = Exporter {
        fail_at: Some(3),
        ..Default::default()
    };
    assert!(export_spans(&*runtime.store(), &root, &mut exporter).is_err());
    assert_eq!(
        exporter.events,
        vec![
            "start:1:None",
            "start:2:Some(1)",
            "end:2:true",
            "end:1:true"
        ]
    );
}
