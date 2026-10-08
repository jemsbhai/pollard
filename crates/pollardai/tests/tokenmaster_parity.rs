use pollardai::{tokenmaster::*, *};
use std::rc::Rc;

fn fixtures() -> Value {
    serde_json::from_str(include_str!("pypi160_tokenmaster020.json")).unwrap()
}
fn registry(f: &Value) -> Rc<ProfileRegistry> {
    Rc::new(ProfileRegistry::from_value(&f["catalog"]).unwrap())
}
fn equal(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(a), Value::Number(b)) => {
            let a = a.as_f64().unwrap();
            let b = b.as_f64().unwrap();
            assert!(
                (a - b).abs() <= 1e-12 * b.abs().max(1.0),
                "{path}: {a} != {b}"
            );
        }
        (Value::Object(a), Value::Object(b)) => {
            assert_eq!(a.len(), b.len(), "{path}: {actual} != {expected}");
            for (key, value) in b {
                equal(
                    a.get(key).unwrap_or_else(|| panic!("{path}.{key} missing")),
                    value,
                    &format!("{path}.{key}"),
                );
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            assert_eq!(a.len(), b.len(), "{path}");
            for (i, (a, b)) in a.iter().zip(b).enumerate() {
                equal(a, b, &format!("{path}[{i}]"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}"),
    }
}
fn capacity(value: &Value) -> CapacityKind {
    if value.as_str() == Some("effective") {
        CapacityKind::Effective
    } else {
        CapacityKind::Nominal
    }
}
fn estimator(config: &Value) -> TokenEstimator {
    let count = config["estimate"].as_u64();
    Rc::new(move |_| Ok(count))
}
fn token_meter(registry: Rc<ProfileRegistry>, config: &Value) -> TokenmasterMeter {
    let mut meter = TokenmasterMeter::new(registry)
        .with_estimator(estimator(config))
        .with_reserved_output(config["reserved"].as_u64().unwrap_or(0));
    if let Some(model) = config["model"].as_str() {
        meter = meter.with_model(model);
    }
    if config["enforce"].as_bool().unwrap_or(false) {
        meter = meter
            .with_profile_limits(capacity(&config["capacity"]))
            .unwrap();
    }
    if let Some(turns) = config["turns"].as_u64() {
        meter = meter.with_expected_remaining_turns(turns);
    }
    meter
}
fn cost_meter(registry: Rc<ProfileRegistry>, config: &Value) -> TokenmasterCostMeter {
    let mut meter = TokenmasterCostMeter::new(registry, estimator(config))
        .with_reserved_output(config["reserved"].as_u64().unwrap_or(0));
    if let Some(model) = config["model"].as_str() {
        meter = meter.with_model(model);
    }
    meter
}

#[test]
fn frozen_profile_aliases_and_request_boundaries() {
    let f = fixtures();
    let registry = registry(&f);
    assert_eq!(f["provenance"]["tokenmaster_version"], json!("0.2.0"));
    assert_eq!(
        f["provenance"]["pollard_wheel_sha256"],
        json!("569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f")
    );
    for row in f["aliases"].as_array().unwrap() {
        equal(
            &serde_json::to_value(registry.get(row["model"].as_str().unwrap()).unwrap()).unwrap(),
            &row["profile"],
            "alias",
        );
    }
    for (i, row) in f["limits"].as_array().unwrap().iter().enumerate() {
        let check = check_request_limits(
            registry.get(row["model"].as_str().unwrap()).unwrap(),
            row["input"].as_u64().unwrap(),
            row["requested"].as_u64(),
            row["reserved"].as_u64().unwrap(),
            capacity(&row["capacity"]),
        )
        .unwrap();
        equal(
            &serde_json::to_value(check).unwrap(),
            &row["check"],
            &format!("limits[{i}]"),
        );
    }
}

#[test]
fn frozen_tier_quotes_and_provider_exclusive_categories() {
    let f = fixtures();
    let registry = registry(&f);
    for (i, row) in f["quotes"].as_array().unwrap().iter().enumerate() {
        let quote = quote_estimate(
            &registry,
            row["model"].as_str().unwrap(),
            row["input"].as_u64().unwrap(),
            row["reserved"].as_u64().unwrap(),
            row["conservative"].as_bool().unwrap(),
        );
        if row.get("error").is_some() {
            assert!(quote.is_err(), "quote[{i}]");
        } else {
            equal(
                &quote.unwrap().metadata,
                &row["quote"],
                &format!("quote[{i}]"),
            );
        }
    }
    for (i, row) in f["usage"].as_array().unwrap().iter().enumerate() {
        let usage = exclusive_usage(&row["result"]);
        equal(
            &serde_json::to_value(&usage).unwrap(),
            &row["exclusive"],
            &format!("usage[{i}]"),
        );
        for expected in row["quotes"].as_array().unwrap() {
            let quote = quote_usage(&registry, expected["model"].as_str().unwrap(), &usage);
            if expected.get("error").is_some() {
                assert!(quote.is_err());
            } else {
                equal(
                    &quote.unwrap().metadata,
                    &expected["quote"],
                    &format!("usagequote[{i}]"),
                );
            }
        }
    }
}

#[test]
fn frozen_meter_prechecks_and_auditable_refusals() {
    let f = fixtures();
    let registry = registry(&f);
    for (i, row) in f["prechecks"].as_array().unwrap().iter().enumerate() {
        let meter: Box<dyn Meter> = if row["cost"].as_bool().unwrap() {
            Box::new(cost_meter(registry.clone(), &row["config"]))
        } else {
            Box::new(token_meter(registry.clone(), &row["config"]))
        };
        let actual = meter.estimate(NodeKind::ModelCall, &row["payload"]);
        if row.get("invalid").is_some() {
            assert!(
                matches!(actual, Err(Error::Invalid(_))),
                "case{i}: {actual:?}"
            );
            continue;
        }
        if let Some(expected) = row.get("refusal") {
            let Err(Error::MeterPrecheckRefusal(refusal)) = actual else {
                panic!("case{i}: expected refusal, got {actual:?}");
            };
            assert_eq!(
                refusal.reason,
                expected["reason"].as_str().unwrap(),
                "case{i}"
            );
            assert_eq!(json!(refusal.requested), expected["requested"], "case{i}");
            assert_eq!(json!(refusal.remaining), expected["remaining"], "case{i}");
            let mut actual_meta = refusal.audit_meta;
            let mut expected_meta = expected["audit_meta"].clone();
            for field in ["limits", "pricing"] {
                if expected_meta["tokenmaster"][field].get("error").is_some() {
                    assert!(actual_meta["tokenmaster"][field]["error"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty()));
                    actual_meta["tokenmaster"][field]
                        .as_object_mut()
                        .unwrap()
                        .remove("error");
                    expected_meta["tokenmaster"][field]
                        .as_object_mut()
                        .unwrap()
                        .remove("error");
                }
            }
            equal(&actual_meta, &expected_meta, &format!("refusal[{i}]"));
        } else {
            let actual = actual.unwrap();
            let expected = row["estimate"].as_str().map(|s| s.parse::<f64>().unwrap());
            assert_eq!(actual, expected, "case{i}");
        }
    }
}

#[test]
fn frozen_stateful_gauges_advice_and_settlement_metadata() {
    let f = fixtures();
    let registry = registry(&f);
    for (i, sequence) in f["sequences"].as_array().unwrap().iter().enumerate() {
        let token = token_meter(registry.clone(), &sequence["config"]);
        let cost = cost_meter(registry.clone(), &sequence["config"]);
        for (j, row) in sequence["rows"].as_array().unwrap().iter().enumerate() {
            let mut meta = json!({"sentinel":"keep"});
            let amount = cost
                .charge_with_meta(NodeKind::ModelCall, &json!({}), &row["result"], &mut meta)
                .unwrap();
            let tokens = token
                .charge_with_meta(NodeKind::ModelCall, &json!({}), &row["result"], &mut meta)
                .unwrap();
            assert_eq!(tokens, row["tokens"].as_f64().unwrap());
            assert_eq!(
                amount,
                row["cost"].as_str().unwrap().parse::<f64>().unwrap()
            );
            if let Some(turn) = meta
                .get_mut("tokenmaster")
                .and_then(|v| v.get_mut("turn"))
                .and_then(Value::as_object_mut)
            {
                let timestamp = turn.remove("timestamp").unwrap();
                assert!(timestamp.as_str().unwrap().ends_with("+00:00"));
            }
            equal(&meta, &row["meta"], &format!("sequence[{i}][{j}]"));
            assert_eq!(
                json!(cost.precheck_fallback_reason(
                    NodeKind::ModelCall,
                    &json!({}),
                    &row["result"],
                    &meta
                )),
                row["fallback"]
            );
        }
    }
}

#[test]
fn profile_validation_alias_overrides_and_safe_settlement_errors() {
    let mut registry = ProfileRegistry::default();
    let profile=ModelProfile::from_value(json!({"model_id":"test:one","provider":"test","window_nominal":1000,"pricing":{"input":1,"output":2}})).unwrap();
    let mut schedule = PricingSchedule {
        base: profile.pricing.clone().unwrap(),
        tiers: vec![PricingTier {
            min_input_tokens: 10,
            pricing: profile.pricing.clone().unwrap(),
        }],
        scope: PricingScope::default(),
    };
    registry
        .register_with_schedule(profile.clone(), schedule.clone(), &["alias".into()])
        .unwrap();
    assert_eq!(
        registry.get("test:alias-20260101").unwrap().model_id,
        "test:one"
    );
    schedule.tiers.push(schedule.tiers[0].clone());
    assert!(schedule.validate().is_err());
    registry.register(profile, &[]).unwrap();
    assert!(registry.pricing_schedule("alias").unwrap().tiers.is_empty());
    assert!(ModelProfile::from_value(
        json!({"model_id":"invalid","provider":"test","window_nominal":0})
    )
    .is_err());
    let registry = Rc::new(registry);
    assert!(TokenmasterMeter::new(registry.clone())
        .with_profile_limits(CapacityKind::Nominal)
        .is_err());
    let meter = TokenmasterMeter::new(registry)
        .with_model("alias")
        .with_advisor(Rc::new(|_, _| {
            Err(Error::Handler("advisor unavailable".into()))
        }));
    let mut meta = json!({"tokenmaster":{"keep":1}});
    assert_eq!(
        meter
            .charge_with_meta(
                NodeKind::ModelCall,
                &json!({}),
                &json!({"usage":{"input_tokens":10,"output_tokens":2}}),
                &mut meta
            )
            .unwrap(),
        12.0
    );
    assert_eq!(
        meta["tokenmaster"]["meter"],
        json!({"status":"error","error":"advisor unavailable"})
    );
    assert_eq!(meta["tokenmaster"]["keep"], json!(1));
}

#[test]
fn runtime_profile_refuses_before_dispatch_and_pricing_failure_keeps_estimate() {
    let f = fixtures();
    let registry = registry(&f);
    let token = token_meter(
        registry.clone(),
        &json!({"model":"gpt-5.6","estimate":922001,"enforce":true}),
    );
    let runtime =
        Runtime::memory(ReplayMode::Record).with_meters(vec![Rc::new(StepMeter), Rc::new(token)]);
    let mut run = runtime.run("profile-refusal", None, 0).unwrap();
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!(
            "refused request dispatched"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    let cost = cost_meter(registry, &json!({"estimate":10,"reserved":2}));
    let expected = cost
        .estimate(NodeKind::ModelCall, &json!({"model":"gpt-5.6"}))
        .unwrap()
        .unwrap();
    let runtime =
        Runtime::memory(ReplayMode::Record).with_meters(vec![Rc::new(StepMeter), Rc::new(cost)]);
    let mut run = runtime.run("pricing-fallback", None, 0).unwrap();
    let node=run.model_call(json!({"model":"gpt-5.6"}),CallOptions::default(),|_|Ok(json!({"model":"unknown:provider-result","usage":{"input_tokens":10,"output_tokens":2}}))).unwrap();
    assert_eq!(node.meta["charges"]["usd"].as_f64().unwrap(), expected);
    assert_eq!(
        node.meta["accounting_fallbacks"]["usd"],
        json!({"reason":"exact_pricing_unavailable","source":"precheck_estimate"})
    );
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    replay
        .run("pricing-fallback", None, 0)
        .unwrap()
        .model_call(json!({"model":"gpt-5.6"}), CallOptions::default(), |_| {
            panic!("strict replay dispatched")
        })
        .unwrap();
}
