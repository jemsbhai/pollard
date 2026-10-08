use pollardai::*;
use std::sync::Arc;

fn fixture() -> Value {
    serde_json::from_str(include_str!("pypi160_identity_registry.json")).unwrap()
}

#[test]
fn exact_pypi_160_schema_acceptance_expansion_validation_and_redaction() {
    for case in fixture()["schemas"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let result = ActionSpec::new(
            name,
            "1",
            "Release differential fixture.",
            case["schema"].clone(),
            false,
            None,
        );
        if case["accepted"] == false {
            assert!(
                result.is_err(),
                "{name}: Python rejected {}",
                case["schema"]
            );
            continue;
        }
        let spec = result.unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(spec.schema(), &case["resolved_schema"], "{name}");
        assert_eq!(spec.spec_digest(), case["spec_digest"], "{name}");
        for validation in case["validations"].as_array().unwrap() {
            let finding = match spec.validate_args(&validation["args"]) {
                Ok(()) => Value::Null,
                Err(Error::Invalid(detail)) => json!(detail),
                Err(error) => panic!("{name}: {error}"),
            };
            assert_eq!(
                finding, validation["finding"],
                "{name}: {}",
                validation["args"]
            );
            if validation.get("redaction_error").is_some() {
                assert!(spec.redact_args(&validation["args"]).is_err(), "{name}");
            } else {
                assert_eq!(
                    spec.redact_args(&validation["args"]).unwrap(),
                    validation["redacted"],
                    "{name}"
                );
            }
        }
    }
}

#[test]
fn exact_pypi_160_registry_order_digest_and_callback_independence() {
    let specs: Vec<_> = ["z", "a", "m"]
        .into_iter()
        .map(|name| ActionSpec::new(name, "1", "", json!({}), false, Some(Arc::new(Ok))).unwrap())
        .collect();
    let registry = Registry::new(specs.clone()).unwrap();
    assert_eq!(
        registry.iter().map(|spec| spec.name()).collect::<Vec<_>>(),
        ["z", "a", "m"]
    );
    assert_eq!(registry.registry_digest(), fixture()["registry"]["digest"]);
    assert!(registry.contains("a"));
    assert!(!registry.contains("missing"));
    assert!(registry.get("a", Some("2")).is_err());
    assert!(registry.get("missing", None).is_err());
    assert!(Registry::new(vec![specs[0].clone(), specs[0].clone()]).is_err());
    let reversed = Registry::new(specs.into_iter().rev().collect()).unwrap();
    assert_eq!(registry.registry_digest(), reversed.registry_digest());
    for name in ["z", "a", "m"] {
        assert_eq!(
            registry.get(name, None).unwrap().spec_digest(),
            ActionSpec::new(name, "1", "", json!({}), false, None)
                .unwrap()
                .spec_digest()
        );
    }
}

#[test]
fn refs_preserve_annotations_and_do_not_mutate_caller_schema() {
    let schema = json!({"$defs":{"secret":{"type":"string","description":"original"}},"properties":{"secret":{"$ref":"#/$defs/secret","description":"override","sensitive":true}}});
    let original = schema.clone();
    assert!(schema_has_local_refs(&schema));
    let resolved = resolve_local_refs(&schema).unwrap();
    assert_eq!(schema, original);
    assert_eq!(resolved["properties"]["secret"]["description"], "override");
    assert!(!schema_has_local_refs(&resolved));
    let expanded = ActionSpec::new("x", "1", "", resolved, false, None).unwrap();
    let referenced = ActionSpec::new("x", "1", "", schema, false, None).unwrap();
    assert_eq!(expanded.spec_digest(), referenced.spec_digest());
    let args = json!({"secret":"original"});
    let redacted = referenced.redact_args(&args).unwrap();
    assert!(is_redacted(&redacted["secret"]));
    assert_eq!(args["secret"], "original");
}

#[test]
fn decision_uses_python_wire_values() {
    for (decision, text) in [
        (Decision::Allow, "allow"),
        (Decision::Deny, "deny"),
        (Decision::Confirm, "confirm"),
    ] {
        assert_eq!(serde_json::to_value(decision).unwrap(), text);
        assert_eq!(
            serde_json::from_value::<Decision>(json!(text)).unwrap(),
            decision
        );
    }
    assert!(serde_json::from_value::<Decision>(json!("ALLOW")).is_err());
}

#[test]
fn policy_context_contains_custom_fractional_meter_spend() {
    struct CustomMeter;
    impl Meter for CustomMeter {
        fn name(&self) -> &str {
            "custom"
        }
        fn charge(
            &self,
            _kind: NodeKind,
            _payload: &Value,
            _result: &Value,
            _meta: &Value,
        ) -> Result<f64> {
            Ok(0.125)
        }
    }
    let spec = ActionSpec::new(
        "tool",
        "1",
        "",
        json!({}),
        false,
        Some(Arc::new(|_| Ok(json!({})))),
    )
    .unwrap();
    let registry = Registry::new(vec![spec]).unwrap();
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_meter(CustomMeter)
        .with_registry(registry)
        .with_policy(Arc::new(|context| {
            assert_eq!(context.counters.get("custom"), Some(&0.125));
            assert_eq!(context.counters.get("steps"), Some(&1.0));
            Decision::Allow
        }));
    let mut run = runtime.run("policy-counters", None, 0).unwrap();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    run.registered_tool_call("tool", None, json!({}), CallOptions::default())
        .unwrap();
}

#[test]
fn async_handler_identity_is_stable_and_sync_dispatch_is_closed() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let spec =
        ActionSpec::new("async-tool", "1", "", json!({"type":"object"}), false, None).unwrap();
    let digest = spec.spec_digest().to_owned();
    let with_async = spec.with_async_handler(Arc::new(move |_| {
        counter.fetch_add(1, Ordering::SeqCst);
        Box::pin(async { Ok(json!({"text":"done"})) })
    }));
    assert_eq!(with_async.spec_digest(), digest);
    let runtime =
        Runtime::memory(ReplayMode::Record).with_registry(Registry::new(vec![with_async]).unwrap());
    let mut run = runtime.run("sync-async-handler", None, 0).unwrap();
    assert!(matches!(
        run.registered_tool_call("async-tool", None, json!({}), CallOptions::default()),
        Err(Error::PolicyViolation { .. })
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}
