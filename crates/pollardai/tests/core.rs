use pollardai::*;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("vectors.json")).unwrap()
}
fn result(tokens: u64) -> Value {
    json!({"text":"ok","usage":{"input_tokens":tokens,"output_tokens":0}})
}
fn options(tokens: u64) -> CallOptions {
    CallOptions {
        estimated_tokens: Some(tokens),
        ..Default::default()
    }
}
fn kind(value: &Value) -> NodeKind {
    serde_json::from_value(value.clone()).unwrap()
}
fn import(value: &Value) -> Node {
    Node::from_storage(
        value["id"].as_str().unwrap().into(),
        value["parent"].as_str().map(str::to_owned),
        kind(&value["kind"]),
        value["attempt"].as_u64().unwrap(),
        &value["payload"].to_string(),
        value["result_text"].as_str().map(str::to_owned),
        value["result_digest"].as_str().map(str::to_owned),
        &value["meta"].to_string(),
    )
    .unwrap()
}

#[test]
fn shared_python_golden_vectors() {
    let f = fixture();
    for case in f["canonical_cases"].as_array().unwrap() {
        assert_eq!(
            String::from_utf8(canonical_bytes(&case["value"]).unwrap()).unwrap(),
            case["canonical_text"].as_str().unwrap(),
            "{}",
            case["name"]
        );
    }
    for case in f["node_cases"].as_array().unwrap() {
        assert_eq!(
            node_id(
                case["kind"].as_str().unwrap(),
                case["parent"].as_str(),
                case["attempt"].as_u64().unwrap(),
                &case["payload"]
            )
            .unwrap(),
            case["id"].as_str().unwrap(),
            "{}",
            case["name"]
        );
    }
    for case in f["result_cases"].as_array().unwrap() {
        assert_eq!(
            result_digest_from_text(case["result_text"].as_str().unwrap()),
            case["result_digest"].as_str().unwrap()
        );
    }
    for case in f["redaction_cases"].as_array().unwrap() {
        assert_eq!(
            redact(&case["value"], case["hint"].as_str()).unwrap(),
            case["marker"]
        );
    }
    let mut specs = Vec::new();
    for case in f["registry"]["specs"].as_array().unwrap() {
        let identity = &case["identity"];
        let spec = ActionSpec::new(
            identity["name"].as_str().unwrap(),
            identity["version"].as_str().unwrap(),
            identity["description"].as_str().unwrap(),
            identity["schema"].clone(),
            identity["side_effects"].as_bool().unwrap(),
            Some(Arc::new(Ok)),
        )
        .unwrap();
        assert_eq!(spec.spec_digest(), case["spec_digest"].as_str().unwrap());
        specs.push(spec);
    }
    let registry = Registry::new(specs).unwrap();
    assert_eq!(
        registry.registry_digest(),
        f["registry"]["registry_digest"].as_str().unwrap()
    );
    for case in f["registry"]["registered_tools"].as_array().unwrap() {
        let spec = registry.get(case["name"].as_str().unwrap(), None).unwrap();
        assert_eq!(spec.redact_args(&case["args"]).unwrap(), case["audit_args"]);
        let runtime = Runtime::memory(ReplayMode::Record).with_registry(registry.clone());
        let mut run = runtime.run("golden", None, 0).unwrap();
        assert_eq!(
            run.registered_tool_call(
                spec.name(),
                None,
                case["args"].clone(),
                CallOptions::default()
            )
            .unwrap()
            .id,
            case["node"]["id"].as_str().unwrap()
        );
    }
}

#[test]
fn identity_rejects_nonportable_values_and_invalid_node_shapes() {
    for value in [
        json!(1.0),
        json!(-0.0),
        json!({"n":MAX_SAFE_INTEGER+1}),
        json!(-(MAX_SAFE_INTEGER as i64) - 1),
        json!(u64::MAX),
    ] {
        assert!(canonical_bytes(&value).is_err());
    }
    assert!(serde_json::from_str::<Value>("\"\\ud800\"").is_err());
    assert!(Node::make(NodeKind::Note, None, 0, json!({}), None, json!({})).is_err());
    assert!(Node::make(
        NodeKind::Root,
        Some(&"a".repeat(64)),
        0,
        json!({}),
        None,
        json!({})
    )
    .is_err());
    assert!(Node::make(
        NodeKind::Root,
        None,
        MAX_SAFE_INTEGER + 1,
        json!({}),
        None,
        json!({})
    )
    .is_err());
    assert!(Budget {
        steps: Some(MAX_SAFE_INTEGER + 1),
        ..Default::default()
    }
    .validate()
    .is_err());
}

#[test]
fn imported_results_preserve_exact_text_and_store_is_detached() {
    let f = fixture();
    let mut store = MemoryStore::new();
    let mut root = import(&f["detached_store"]["root_before"]);
    let root_id = root.id.clone();
    store.put(root.clone()).unwrap();
    root.payload["run"] = json!("mutated");
    root.meta["nested"]["value"] = json!("mutated");
    assert_eq!(
        store.get(&root_id).unwrap(),
        import(&f["detached_store"]["root_after_mutations"])
    );
    let mut got = store.get(&root_id).unwrap();
    got.payload["run"] = json!("changed");
    assert_ne!(store.get(&root_id).unwrap().payload, got.payload);
    let mut child = import(&f["detached_store"]["child"]);
    let text = f["result_cases"][2]["result_text"].as_str().unwrap();
    child.result_text = Some(text.into());
    child.result_digest = Some(result_digest_from_text(text));
    store.put(child.clone()).unwrap();
    assert_eq!(
        store.get(&child.id).unwrap().result_text.as_deref(),
        Some(text)
    );
    assert!(verify(&store, &child.id).ok);
    child.result_text = Some("{\"a\":7}".into());
    assert!(store.put(child).is_err());
    assert!(!verify(&store, &"a".repeat(64)).ok);
}

#[test]
fn model_and_tool_callback_mutation_cannot_change_identity() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("isolation", None, 0).unwrap();
    let payload = json!({"model":"test","messages":[{"text":"original"}]});
    let node = run
        .model_call(payload.clone(), CallOptions::default(), |mut got| {
            got["messages"][0]["text"] = json!("mutated");
            Ok(result(1))
        })
        .unwrap();
    assert_eq!(node.payload, payload);
    assert!(verify(&*runtime.store(), &node.id).ok);
    let tool = run
        .tool_call(
            "test",
            json!({"value":1}),
            CallOptions::default(),
            |mut got| {
                got["args"]["value"] = json!(2);
                Ok(result(0))
            },
        )
        .unwrap();
    assert_eq!(tool.payload["args"]["value"], json!(1));
}

#[test]
fn budgets_refuse_before_side_effect_and_charge_actual_overshoot() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "budget",
            Some(Budget {
                steps: Some(5),
                tokens: Some(3),
                depth: Some(6),
            }),
            0,
        )
        .unwrap();
    let node = run
        .model_call(json!({"model":"x"}), options(2), |_| Ok(result(4)))
        .unwrap();
    assert_eq!(node.meta["charges"]["tokens"], json!(4));
    let calls = AtomicUsize::new(0);
    let err = run
        .model_call(json!({"model":"y"}), options(0), |_| {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(result(0))
        })
        .unwrap_err();
    assert!(matches!(err,Error::BudgetExceeded{meter,..} if meter=="tokens"));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(
        run.spent().unwrap(),
        Charges {
            steps: 1,
            tokens: 4
        }
    );
    let mut zero = runtime
        .run(
            "zero",
            Some(Budget {
                steps: Some(0),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        zero.tool_call("t", json!({}), CallOptions::default(), |_| panic!(
            "must not call"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    let mut depth = runtime
        .run(
            "depth",
            Some(Budget {
                depth: Some(0),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(
        matches!(depth.note(json!({"note":1}),0),Err(Error::BudgetExceeded{meter,..}) if meter=="depth")
    );
}

#[test]
fn malformed_usage_fail_closes_after_dispatch_and_keeps_conservative_charge() {
    for (i, usage) in [
        Value::Null,
        json!({"input_tokens":-10,"output_tokens":0}),
        json!({"input_tokens":true,"output_tokens":0}),
        json!({"input_tokens":0.5,"output_tokens":1}),
        json!({"input_tokens":u64::MAX,"output_tokens":1}),
    ]
    .into_iter()
    .enumerate()
    {
        let runtime = Runtime::memory(ReplayMode::Record);
        let mut run = runtime
            .run(
                format!("invalid{i}"),
                Some(Budget {
                    tokens: Some(10),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        assert!(matches!(
            run.model_call(json!({}), options(3), |_| Ok(json!({"usage":usage}))),
            Err(Error::UsageError { .. })
        ));
        assert_eq!(
            run.spent().unwrap(),
            Charges {
                steps: 1,
                tokens: 3
            }
        );
        assert!(matches!(
            run.model_call(json!({"next":true}), options(0), |_| panic!(
                "unknown usage cannot grant credits"
            )),
            Err(Error::BudgetExceeded { .. })
        ));
    }
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "need-estimate",
            Some(Budget {
                tokens: Some(10),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!(
            "estimate required"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}

#[test]
fn duplicate_recordings_and_failed_calls_never_redispatch() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("duplicate", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let payload = json!({"model":"x"});
    run.model_call(payload.clone(), CallOptions::default(), |_| Ok(result(0)))
        .unwrap();
    run.rollback(&root).unwrap();
    assert!(matches!(
        run.model_call(payload.clone(), CallOptions::default(), |_| panic!(
            "duplicate"
        )),
        Err(Error::DuplicateRecording(_))
    ));
    run.model_call(
        payload,
        CallOptions {
            attempt: 1,
            ..Default::default()
        },
        |_| Ok(result(0)),
    )
    .unwrap();
    let mut fail = runtime
        .run(
            "failed",
            Some(Budget {
                steps: Some(2),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let root = fail.root_id().to_owned();
    assert!(matches!(
        fail.model_call(json!({}), options(2), |_| Err(Error::Handler(
            "test".into()
        ))),
        Err(Error::Handler(_))
    ));
    assert_eq!(
        fail.spent().unwrap(),
        Charges {
            steps: 1,
            tokens: 2
        }
    );
    fail.rollback(&root).unwrap();
    assert!(matches!(
        fail.model_call(json!({}), CallOptions::default(), |_| panic!(
            "failed redispatch"
        )),
        Err(Error::DuplicateRecording(_))
    ));
}

#[test]
fn reentrant_runtimes_on_shared_store_are_locked() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let other = Runtime::from_shared(runtime.shared_store(), ReplayMode::Record);
    let mut run = runtime.run("reentrant", None, 0).unwrap();
    run.model_call(json!({}), CallOptions::default(), |_| {
        assert!(matches!(other.run("other", None, 0), Err(Error::Busy)));
        Ok(result(0))
    })
    .unwrap();
    assert!(other.run("other", None, 0).is_ok());
}

#[test]
fn panic_leaves_pending_work_charged_and_cannot_redispatch() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("panic", None, 0).unwrap();
    let root = run.root_id().to_owned();
    assert!(
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run.model_call(
            json!({}),
            options(2),
            |_| panic!("provider panic")
        )))
        .is_err()
    );
    assert_eq!(
        run.spent().unwrap(),
        Charges {
            steps: 1,
            tokens: 2
        }
    );
    run.rollback(&root).unwrap();
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!("redispatch")),
        Err(Error::DuplicateRecording(_))
    ));
}

#[test]
fn strict_and_hybrid_replay_never_execute_handlers_and_missing_is_closed() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut record = runtime.run("replay", None, 0).unwrap();
    let payload = json!({"model":"x"});
    let recorded = record
        .model_call(payload.clone(), CallOptions::default(), |_| Ok(result(3)))
        .unwrap();
    for mode in [ReplayMode::Replay, ReplayMode::Hybrid] {
        let replay = Runtime::from_shared(runtime.shared_store(), mode);
        let mut run = replay
            .run(
                "replay",
                Some(Budget {
                    steps: Some(0),
                    tokens: Some(0),
                    depth: Some(0),
                }),
                0,
            )
            .unwrap();
        assert_eq!(
            run.model_call(payload.clone(), CallOptions::default(), |_| panic!(
                "replay callback"
            ))
            .unwrap(),
            recorded
        );
        assert_eq!(
            run.avoided(),
            Charges {
                steps: 1,
                tokens: 3
            }
        );
    }
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut run = replay.run("replay", None, 0).unwrap();
    assert!(matches!(
        run.model_call(
            json!({"model":"missing"}),
            CallOptions::default(),
            |_| panic!("missing replay callback")
        ),
        Err(Error::MissingRecording(_))
    ));
    assert!(matches!(
        replay.run("missing", None, 0),
        Err(Error::MissingRecording(_))
    ));
}

#[derive(Clone)]
struct Tampered(MemoryStore);
impl Store for Tampered {
    fn put(&mut self, n: Node) -> Result<()> {
        self.0.put(n)
    }
    fn get(&self, id: &str) -> Result<Node> {
        let mut node = self.0.get(id)?;
        if node.kind == NodeKind::ModelCall {
            node.result_text = Some("{}".into());
        }
        Ok(node)
    }
    fn exists(&self, id: &str) -> bool {
        self.0.exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.0.children(id)
    }
    fn update_meta(&mut self, id: &str, v: Value) -> Result<()> {
        self.0.update_meta(id, v)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.0.roots()
    }
}
impl RecordingStore for Tampered {
    fn finalize(&mut self, n: Node) -> Result<()> {
        self.0.finalize(n)
    }
}

#[test]
fn tampered_replay_is_rejected() {
    let mut store = MemoryStore::new();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"tamper"}),
        None,
        json!({}),
    )
    .unwrap();
    let child = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"model":"x"}),
        Some(result(0)),
        json!({}),
    )
    .unwrap();
    store.put(root).unwrap();
    store.put(child).unwrap();
    let runtime = Runtime::new(Tampered(store), ReplayMode::Replay);
    let mut run = runtime.run("tamper", None, 0).unwrap();
    assert!(matches!(
        run.model_call(json!({"model":"x"}), CallOptions::default(), |_| panic!(
            "tamper callback"
        )),
        Err(Error::Integrity(_))
    ));
}

#[test]
fn branches_share_charges_and_rollback_does_not_refund() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "branch",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let root = run.root_id().to_owned();
    let mut branch = run
        .branch(
            0,
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
        )
        .unwrap();
    branch
        .model_call(json!({}), CallOptions::default(), |_| Ok(result(0)))
        .unwrap();
    assert_eq!(run.cursor_id(), root);
    run.adopt(&branch).unwrap();
    run.rollback(&root).unwrap();
    assert_eq!(run.spent().unwrap().steps, 1);
    assert!(matches!(
        run.model_call(json!({"next":1}), CallOptions::default(), |_| panic!(
            "budget refunded"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}

fn spec(handler: Option<Handler>) -> ActionSpec {
    ActionSpec::new("send","1","Send",json!({"type":"object","properties":{"secret":{"type":"string","sensitive":true}},"required":["secret"],"additionalProperties":false}),true,handler).unwrap()
}

#[test]
fn registry_schema_policy_and_redaction_gate_dispatch() {
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let registry = Registry::new(vec![spec(Some(Arc::new(move |mut args| {
        counter.fetch_add(1, Ordering::SeqCst);
        assert_eq!(args["secret"], json!("private"));
        args["secret"] = json!("changed");
        Ok(result(0))
    })))])
    .unwrap();
    let runtime = Runtime::memory(ReplayMode::Record).with_registry(registry.clone());
    let mut run = runtime.run("registry", None, 0).unwrap();
    let node = run
        .registered_tool_call(
            "send",
            Some("1"),
            json!({"secret":"private"}),
            CallOptions::default(),
        )
        .unwrap();
    assert!(node.payload["args"]["secret"].is_object());
    assert!(!node.payload.to_string().contains("private"));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    for (name, version, args) in [
        ("unknown", None, json!({})),
        ("send", Some("2"), json!({"secret":"private"})),
        ("send", None, json!({"secret":"private","extra":1})),
    ] {
        assert!(matches!(
            run.registered_tool_call(name, version, args, CallOptions::default()),
            Err(Error::PolicyViolation { .. })
        ));
    }
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let deny = Runtime::memory(ReplayMode::Record)
        .with_registry(registry)
        .with_policy(Arc::new(|_| Decision::Deny));
    let mut denied = deny.run("deny", None, 0).unwrap();
    assert!(matches!(
        denied.registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default()
        ),
        Err(Error::PolicyViolation { .. })
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[test]
fn confirmation_is_single_use_and_rechecks_budget_at_dispatch() {
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    let registry = Registry::new(vec![spec(Some(Arc::new(move |_| {
        c.fetch_add(1, Ordering::SeqCst);
        Ok(result(0))
    })))])
    .unwrap();
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_registry(registry)
        .with_policy(Arc::new(|_| Decision::Confirm));
    let mut run = runtime.run("confirm", None, 0).unwrap();
    let token = match run
        .registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default(),
        )
        .unwrap_err()
    {
        Error::ConfirmationRequired { token } => token,
        e => panic!("{e}"),
    };
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    run.confirm(&token).unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(run.confirm(&token).is_err());
    let mut moved = runtime.run("moved", None, 0).unwrap();
    let token = match moved
        .registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default(),
        )
        .unwrap_err()
    {
        Error::ConfirmationRequired { token } => token,
        e => panic!("{e}"),
    };
    moved.note(json!({"changed":true}), 0).unwrap();
    assert!(moved.confirm(&token).is_err());
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[test]
fn schema_subset_is_fail_closed_and_caller_values_do_not_panic() {
    for schema in [
        json!({"type":"number"}),
        json!({"type":["string","null"]}),
        json!({"$ref":"#/$defs/x"}),
        json!({"type":"string","pattern":".*"}),
        json!({"type":"array","uniqueItems":true}),
        json!({"enum":[]}),
        json!({"enum":[true,true]}),
        json!({"anyOf":[]}),
        json!({"minLength":1}),
        json!({"type":"string","minLength":-1}),
        json!({"type":"integer","sensitive":true}),
    ] {
        assert!(matches!(
            ActionSpec::new("bad", "1", "", schema, false, None),
            Err(Error::UnsupportedSchema(_))
        ));
    }
    let constrained=ActionSpec::new("x","1","",json!({"type":"object","properties":{"value":{"anyOf":[{"type":"integer","minimum":1},{"type":"null"}]},"text":{"type":"string","maxLength":1}},"additionalProperties":false}),false,None).unwrap();
    assert!(constrained.validate_args(&json!({"value":true})).is_err());
    assert!(constrained.validate_args(&json!({"value":0})).is_err());
    assert!(constrained
        .validate_args(&json!({"value":null,"text":"😀"}))
        .is_ok());
    assert!(constrained.redact_args(&json!({"value":u64::MAX})).is_err());
}

#[test]
fn mutable_metadata_cannot_reopen_finalization() {
    let mut store = MemoryStore::new();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"settle"}),
        None,
        json!({}),
    )
    .unwrap();
    store.put(root.clone()).unwrap();
    let pending = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({}),
        None,
        json!({"state":"pending"}),
    )
    .unwrap();
    store.put(pending.clone()).unwrap();
    let mut failed = pending.clone();
    failed.meta = json!({"state":"failed"});
    store.finalize(failed).unwrap();
    store
        .update_meta(&pending.id, json!({"state":"pending"}))
        .unwrap();
    let mut reopened = pending;
    reopened.meta = json!({"state":"completed"});
    assert!(store.finalize(reopened).is_err());
}

#[test]
fn denial_dominates_confirmation_and_hybrid_reuse_skips_policies() {
    let registry = Registry::new(vec![spec(Some(Arc::new(|_| Ok(result(0)))))]).unwrap();
    let denied = Runtime::memory(ReplayMode::Record)
        .with_registry(registry.clone())
        .with_policy(Arc::new(|_| Decision::Confirm))
        .with_policy(Arc::new(|_| Decision::Deny));
    let mut run = denied.run("deny-after-confirm", None, 0).unwrap();
    assert!(matches!(
        run.registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default()
        ),
        Err(Error::PolicyViolation { .. })
    ));
    let record = Runtime::memory(ReplayMode::Record).with_registry(registry.clone());
    let mut run = record.run("hybrid-policy", None, 0).unwrap();
    let node = run
        .registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default(),
        )
        .unwrap();
    let hybrid = Runtime::from_shared(record.shared_store(), ReplayMode::Hybrid)
        .with_registry(registry)
        .with_policy(Arc::new(|_| panic!("hybrid hit policy")));
    let mut run = hybrid.run("hybrid-policy", None, 0).unwrap();
    assert_eq!(
        run.registered_tool_call(
            "send",
            None,
            json!({"secret":"private"}),
            CallOptions::default()
        )
        .unwrap(),
        node
    );
}

#[test]
fn unknown_usage_recorded_result_can_be_strictly_replayed() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "unknown-replay",
            Some(Budget {
                tokens: Some(3),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        run.model_call(json!({"model":"x"}), options(2), |_| Ok(
            json!({"text":"semantic result"})
        )),
        Err(Error::UsageError { .. })
    ));
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut run = replay.run("unknown-replay", None, 0).unwrap();
    assert_eq!(
        run.model_call(json!({"model":"x"}), CallOptions::default(), |_| panic!(
            "replay unknown usage"
        ))
        .unwrap()
        .result
        .unwrap()["text"],
        json!("semantic result")
    );
}
