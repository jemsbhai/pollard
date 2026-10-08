#![cfg(feature = "estimate-openai")]
use pollardai::{estimators::*, *};
#[test]
fn textual_leaf_estimates_match_python_tiktoken_and_reject_special_tokens() {
    let fixture: Value = serde_json::from_str(include_str!("pypi160_estimators.json")).unwrap();
    let mut estimators = std::collections::BTreeMap::new();
    for case in fixture["cases"].as_array().unwrap() {
        let model = case["model"].as_str().map(str::to_owned);
        let estimator = estimators
            .entry(model.clone())
            .or_insert_with(|| OpenAiTokenEstimator::new(model.clone(), 3));
        let result = estimator.estimate_input_tokens(&case["payload"]);
        assert_eq!(
            fallback_encoding_name(
                model
                    .as_deref()
                    .filter(|model| !model.is_empty())
                    .or_else(|| case["payload"].get("model").and_then(Value::as_str))
            ),
            case["fallback"]
        );
        if case.get("error").is_some() {
            assert!(result.is_err(), "{case}");
        } else {
            assert_eq!(result.unwrap(), case["tokens"].as_u64().unwrap(), "{case}");
        }
    }
}
#[test]
fn message_overhead_is_configurable_and_overflow_is_rejected() {
    let payload = json!({"messages":[{},{}]});
    assert_eq!(
        OpenAiTokenEstimator::new(None, 7)
            .estimate_input_tokens(&payload)
            .unwrap(),
        14
    );
    assert!(OpenAiTokenEstimator::new(None, u64::MAX)
        .estimate_input_tokens(&payload)
        .is_err());
    let callback = OpenAiTokenEstimator::default().into_meter_estimator();
    assert_eq!(callback(&payload).unwrap(), Some(6));
}
