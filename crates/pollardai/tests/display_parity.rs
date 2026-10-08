use pollardai::*;

#[test]
fn unusual_json_labels_and_span_names_match_python_repr() {
    let fixtures: Value = serde_json::from_str(include_str!("pypi160_display.json")).unwrap();
    for row in fixtures["cases"].as_array().unwrap() {
        let root = Node::make(
            NodeKind::Root,
            None,
            0,
            json!({"run":"display"}),
            None,
            json!({}),
        )
        .unwrap();
        for key in ["tool", "model", "modelId"] {
            let kind = if key == "tool" {
                NodeKind::ToolCall
            } else {
                NodeKind::ModelCall
            };
            let node = Node::make(
                kind,
                Some(&root.id),
                0,
                json!({key:row["value"]}),
                None,
                json!({}),
            )
            .unwrap();
            let expected = if key == "tool" {
                row["span_name"].as_str().unwrap().to_owned()
            } else {
                format!("chat {}", row["label"].as_str().unwrap())
            };
            assert_eq!(otel::span_name(&node), expected);
        }
        let node = Node::make(
            NodeKind::Refusal,
            Some(&root.id),
            0,
            json!({"reason":row["value"]}),
            None,
            json!({}),
        )
        .unwrap();
        let mut store = MemoryStore::new();
        store.put(root).unwrap();
        store.put(node.clone()).unwrap();
        assert_eq!(
            render::render_ascii(&store, &node.id, false, false).unwrap(),
            format!(
                "refusal {} {} [REFUSED]",
                &node.id[..8],
                row["label"].as_str().unwrap()
            )
        );
    }
}
