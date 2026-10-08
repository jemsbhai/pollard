use pollardai::*;
use std::collections::BTreeSet;

fn fixture() -> Value {
    serde_json::from_str(include_str!("pypi160_hashrope.json")).unwrap()
}
fn check(store: &HashRopeStore, expected: &Value) {
    assert_eq!(
        String::from_utf8(store.to_bytes()).unwrap(),
        expected["log"]
    );
    assert_eq!(store.content_hash(), expected["hash"].as_u64().unwrap());
    store.validate_log().unwrap();
    let nodes: Vec<Node> = serde_json::from_value(expected["nodes"].clone()).unwrap();
    assert_eq!(store.walk(&nodes[0].id).unwrap(), nodes);
    assert!(verify_subtree(store, &nodes[0].id).ok);
    assert_eq!(
        render::render_ascii(store, &nodes[0].id, false, false).unwrap(),
        expected["ascii"]
    );
    assert_eq!(
        render::render_ascii(store, &nodes[0].id, true, false).unwrap(),
        expected["unicode"]
    );
    assert_eq!(
        render::render_ascii(store, &nodes[0].id, false, true).unwrap(),
        expected["ascii_payloads"]
    );
}
#[test]
fn exact_python_hashrope_log_bytes_and_hash() {
    let data = fixture();
    let mut store = HashRopeStore::new();
    for op in data["operations"].as_array().unwrap() {
        match op["op"].as_str().unwrap() {
            "put" => store
                .put(serde_json::from_value(op["node"].clone()).unwrap())
                .unwrap(),
            "meta" => store
                .update_meta(op["id"].as_str().unwrap(), op["patch"].clone())
                .unwrap(),
            _ => unreachable!(),
        }
    }
    check(&store, &data["snapshots"]["recorded"]);
    let mut copy = HashRopeStore::from_bytes(&store.to_bytes()).unwrap();
    check(&copy, &data["snapshots"]["recorded"]);
    assert_eq!(copy.compact().unwrap(), 0);
    check(&copy, &data["snapshots"]["compacted"]);
    copy.drop_nodes(&BTreeSet::from([data["leaf"].as_str().unwrap().to_owned()]))
        .unwrap();
    check(&copy, &data["snapshots"]["dropped"]);
}
#[test]
fn hashrope_imports_blank_lines_and_rejects_malformed_operations() {
    let data = fixture();
    let text = data["snapshots"]["recorded"]["log"].as_str().unwrap();
    let bytes = format!("\n{}\n", text.replace('\n', "\r\n")).into_bytes();
    let store = HashRopeStore::from_bytes(&bytes).unwrap();
    assert_eq!(store.to_bytes(), bytes);
    store.validate_log().unwrap();
    for invalid in [
        b"[]\n".as_slice(),
        b"{\"op\":\"delete\"}\n",
        b"{\"op\":\"put\"}\n",
        b"{\"op\":\"meta\",\"id\":\"x\",\"patch\":{}}\n",
        b"{\"op\":\"meta\",\"id\":\"x\",\"patch\":null}\n",
        b"{",
        b"\xff",
    ] {
        assert!(HashRopeStore::from_bytes(invalid).is_err());
    }
}
#[test]
fn hashrope_preserves_imported_bytes_and_separates_unterminated_records_on_append() {
    let data = fixture();
    for row in data["imports"].as_array().unwrap() {
        let bytes = row["log"].as_str().unwrap().as_bytes();
        let terminated = matches!(bytes.last(), Some(b'\n' | b'\r'));
        let mut store = HashRopeStore::from_bytes(bytes).unwrap();
        assert_eq!(store.to_bytes(), bytes);
        let imported_hash = store.content_hash();
        assert_eq!(imported_hash, row["hash"].as_u64().unwrap());
        store.validate_log().unwrap();
        assert_eq!(store.content_hash(), imported_hash);
        let root = store.roots().unwrap()[0].clone();
        store.update_meta(&root, json!({"imported": true})).unwrap();
        let appended = store.to_bytes();
        assert!(appended.starts_with(bytes));
        if !terminated {
            assert_eq!(appended[bytes.len()], b'\n');
        }
        assert_eq!(store.get(&root).unwrap().meta["imported"], true);
        store.validate_log().unwrap();
        let reopened = HashRopeStore::from_bytes(&appended).unwrap();
        assert_eq!(reopened.content_hash(), store.content_hash());
        assert_eq!(reopened.walk(&root).unwrap(), store.walk(&root).unwrap());
    }
}
#[test]
fn hashrope_stages_pending_but_publishes_only_final_record() {
    let mut store = HashRopeStore::new();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"pending"}),
        None,
        json!({}),
    )
    .unwrap();
    store.put(root.clone()).unwrap();
    let before = store.to_bytes();
    let pending = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"model":"m"}),
        None,
        json!({"state":"pending"}),
    )
    .unwrap();
    store.stage_pending(pending.clone()).unwrap();
    assert_eq!(store.to_bytes(), before);
    assert!(store.exists(&pending.id));
    assert!(!HashRopeStore::from_bytes(&before)
        .unwrap()
        .exists(&pending.id));
    let result = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        pending.payload,
        Some(json!({"text":"ok"})),
        json!({"state":"completed"}),
    )
    .unwrap();
    store.finalize(result.clone()).unwrap();
    assert_eq!(
        store
            .to_bytes()
            .split(|b| *b == b'\n')
            .filter(|s| !s.is_empty())
            .count(),
        2
    );
    assert_eq!(
        HashRopeStore::from_bytes(&store.to_bytes())
            .unwrap()
            .get(&result.id)
            .unwrap(),
        result
    );
    store.validate_log().unwrap();
    assert!(store.finalize(result).is_err());
}
#[test]
fn hashrope_batches_and_orphan_drops_are_atomic() {
    let mut store = HashRopeStore::from_bytes(
        fixture()["snapshots"]["recorded"]["log"]
            .as_str()
            .unwrap()
            .as_bytes(),
    )
    .unwrap();
    let before = store.to_bytes();
    let root = store.roots().unwrap()[0].clone();
    assert!(store.drop_nodes(&BTreeSet::from([root.clone()])).is_err());
    assert!(store
        .apply_batch(
            vec![],
            vec![(root, json!({"ok":true})), ("missing".into(), json!({}))]
        )
        .is_err());
    assert_eq!(store.to_bytes(), before);
}

#[test]
fn hashrope_runtime_records_reopens_and_replays_final_results() {
    let runtime = Runtime::new(HashRopeStore::new(), ReplayMode::Record);
    let mut run = runtime.run("runtime", None, 0).unwrap();
    let node = run
        .model_call(json!({"model":"test"}), CallOptions::default(), |_| {
            Ok(json!({"text":"ok"}))
        })
        .unwrap();
    let bytes = runtime.store().operation_log().unwrap();
    let snapshot = HashRopeStore::from_bytes(&bytes).unwrap();
    snapshot.validate_log().unwrap();
    assert_eq!(snapshot.get(&node.id).unwrap(), node);
    let replay = Runtime::new(snapshot, ReplayMode::Replay);
    let mut cached = replay.run("runtime", None, 0).unwrap();
    assert_eq!(
        cached
            .model_call(json!({"model":"test"}), CallOptions::default(), |_| panic!(
                "replay cannot dispatch"
            ))
            .unwrap(),
        node
    );
    let root = run.root_id().to_owned();
    run.rollback(&root).unwrap();
    run.model_call(json!({"model":"test"}), CallOptions::default(), |_| {
        Ok(json!({"text":"other"}))
    })
    .unwrap();
    let snapshot = HashRopeStore::from_bytes(&runtime.store().operation_log().unwrap()).unwrap();
    assert_eq!(snapshot.get(&node.id).unwrap().result, node.result);
    assert_eq!(
        snapshot.get(&node.id).unwrap().meta["result_conflicts"][0]["result"]["text"],
        "other"
    );
    snapshot.validate_log().unwrap();
}

#[test]
fn hashrope_known_errors_release_identity_but_unknown_errors_are_persisted() {
    let runtime = Runtime::new(HashRopeStore::new(), ReplayMode::Record);
    let mut run = runtime.run("errors", None, 0).unwrap();
    let before = runtime.store().operation_log().unwrap();
    assert!(run
        .model_call(json!({"model":"test"}), CallOptions::default(), |_| Err(
            Error::Handler("not dispatched".into())
        ))
        .is_err());
    assert_eq!(runtime.store().operation_log().unwrap(), before);
    assert!(run
        .model_call(json!({"model":"test"}), CallOptions::default(), |_| Err(
            Error::OutcomeUnknown(Box::new(Error::Handler("uncertain".into())))
        ))
        .is_err());
    let snapshot = HashRopeStore::from_bytes(&runtime.store().operation_log().unwrap()).unwrap();
    let nodes = snapshot.walk(run.root_id()).unwrap();
    assert_eq!(nodes.len(), 2);
    assert_eq!(nodes[1].meta["state"], "failed");
    snapshot.validate_log().unwrap();
}
