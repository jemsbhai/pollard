use pollardai::*;

#[test]
fn exact_pypi160_result_comparison_and_semantic_projection() {
    let fixtures: Value = serde_json::from_str(include_str!("pypi160_revalidation.json")).unwrap();
    for (i, case) in fixtures["comparisons"]
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
    {
        for (comparator, expected) in [
            (
                &ExactResultComparator as &dyn RevalidationComparator,
                "exact",
            ),
            (&NormalizedModelComparator, "normalized"),
        ] {
            let comparison = comparator
                .compare(&case["recorded"], &case["live"])
                .unwrap();
            comparison.validate().unwrap();
            assert_eq!(comparison.to_value(), case[expected], "case {i} {expected}");
        }
    }
}

#[test]
fn exact_pypi160_contract_binding_and_observation_payload() {
    let fixtures: Value = serde_json::from_str(include_str!("pypi160_revalidation.json")).unwrap();
    let recorded = Node::make(
        NodeKind::ModelCall,
        Some(&"a".repeat(64)),
        0,
        json!({"model":"fixed"}),
        Some(json!({"text":"recorded"})),
        json!({}),
    )
    .unwrap();
    for case in fixtures["contracts"].as_array().unwrap() {
        let options = &case["options"];
        let mut contract = ReplayContract::new(options["provider"].as_str().unwrap()).unwrap();
        contract.model_revision = options["model_revision"].as_str().map(str::to_owned);
        contract.api_version = options["api_version"].as_str().map(str::to_owned);
        contract.adapter = options["adapter"].as_str().map(str::to_owned);
        contract.adapter_version = options["adapter_version"].as_str().map(str::to_owned);
        contract.sdk = options["sdk"].as_str().map(str::to_owned);
        contract.sdk_version = options["sdk_version"].as_str().map(str::to_owned);
        contract.application_revision = options["application_revision"].as_str().map(str::to_owned);
        contract.environment = options
            .get("environment")
            .cloned()
            .unwrap_or_else(|| json!({}));
        contract.validate().unwrap();
        assert_eq!(contract.to_value(), case["contract"]);
        let bound = contract.bind(case["payload"].clone()).unwrap();
        assert_eq!(bound, case["bound"]);
        assert_eq!(
            extract_replay_contract(&bound).unwrap().unwrap(),
            case["extracted"]
        );
        assert_eq!(
            make_revalidation_payload(
                &case["payload"],
                "obs-1",
                &recorded,
                &contract,
                "normalized-model/v1"
            )
            .unwrap(),
            case["revalidation_payload"]
        );
    }
}

#[test]
fn contracts_and_comparison_reject_invalid_and_conflicting_audit_metadata() {
    for provider in ["", " ", "\n\t"] {
        assert!(ReplayContract::new(provider).is_err());
    }
    let mut contract = ReplayContract::new("local").unwrap();
    contract.environment = json!({"float":1.0});
    assert!(contract.bind(json!({})).is_err());
    contract.environment = json!({});
    for payload in [
        json!({"_pollard":false}),
        json!({"_pollard":{"replay_contract":{"provider":"other"}}}),
        json!([]),
    ] {
        assert!(contract.bind(payload).is_err());
    }
    assert!(extract_replay_contract(&json!({"_pollard":{"replay_contract":null}})).is_err());
    assert_eq!(
        extract_replay_contract(&json!({"_pollard":false})).unwrap(),
        None
    );
    for (matched, paths, truncated) in [
        (true, vec!["/x".into()], false),
        (true, vec![], true),
        (false, vec!["x".into()], false),
        (false, vec!["/".into(); 101], false),
    ] {
        assert!(RevalidationComparison::new(matched, paths, truncated).is_err());
    }
    assert!(RevalidationComparison::new(false, vec![], false).is_ok());
    let recorded = Node::make(
        NodeKind::ModelCall,
        Some(&"a".repeat(64)),
        0,
        json!({}),
        Some(json!({})),
        json!({}),
    )
    .unwrap();
    assert!(make_revalidation_payload(
        &json!({"_pollard":{"revalidation":null}}),
        "obs",
        &recorded,
        &contract,
        "comparator"
    )
    .is_err());
    assert!(
        make_revalidation_payload(&json!({}), " ", &recorded, &contract, "comparator").is_err()
    );
    assert!(make_revalidation_payload(&json!({}), "obs", &recorded, &contract, "").is_err());
}

#[test]
fn comparators_distinguish_python_numeric_types_and_compare_float_values() {
    let one: Value = serde_json::from_str("{\"x\":1.0}").unwrap();
    let exponent: Value = serde_json::from_str("{\"x\":1e0}").unwrap();
    assert!(
        ExactResultComparator
            .compare(&one, &exponent)
            .unwrap()
            .matched
    );
    assert!(
        !ExactResultComparator
            .compare(&one, &json!({"x":1}))
            .unwrap()
            .matched
    );
    assert!(
        !ExactResultComparator
            .compare(&json!({"x":true}), &json!({"x":1}))
            .unwrap()
            .matched
    );
    let comparison = ExactResultComparator
        .compare(&json!(1), &json!("1"))
        .unwrap();
    assert_eq!(comparison.difference_paths, ["/"]);
}
