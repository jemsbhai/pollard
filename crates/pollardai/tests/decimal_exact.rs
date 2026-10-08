use pollardai::{tokenmaster::*, *};
use rust_decimal::Decimal;
use std::collections::BTreeMap;

#[test]
fn exact_parser_rejects_significant_rounding_and_bounds_exponent_allocation() {
    for input in [
        "1e-29",
        "0.00000000000000000000000000001",
        "1.23456789012345678901234567891",
        "1.23456789012345678901234567891e0",
        "79228162514264337593543950336",
        "1e1000000000",
        "1e-1000000000",
        "1e9223372036854775808",
        "1e--3",
        "1e",
        "e3",
        "NaN",
        "Infinity",
    ] {
        assert!(parse_decimal_exact(input).is_err(), "{input}");
    }
    for (input, expected) in [
        ("1e-28", "0.0000000000000000000000000001"),
        ("10e-29", "0.0000000000000000000000000001"),
        (
            "1.00000000000000000000000000000",
            "1.0000000000000000000000000000",
        ),
        ("1.2300e-3", "0.0012300"),
        (
            "0.12345678901234567890123456789e1",
            "1.2345678901234567890123456789",
        ),
        (
            "79228162514264337593543950335",
            "79228162514264337593543950335",
        ),
        (
            "79228162514264337593543950335.0",
            "79228162514264337593543950335",
        ),
        ("0e1000000000", "0"),
        ("0e-1000000000", "0.0000000000000000000000000000"),
        ("-1.2300e+2", "-123.00"),
        ("+2e+3", "2000"),
    ] {
        assert_eq!(
            parse_decimal_exact(input).unwrap().to_string(),
            expected,
            "{input}"
        );
    }
}

#[test]
fn prices_windows_and_governance_budgets_reject_unrepresentable_inputs() {
    for input in [
        "1e-29",
        "0.00000000000000000000000000001",
        "1.23456789012345678901234567891",
    ] {
        assert!(ModelPrice::new(input, "0").is_err());
        assert!(WindowMeter::new_decimal("requests", input, 1.0, None).is_err());
        let mut refusal = MeterPrecheckRefusal::new("bound").unwrap();
        refusal.requested = Some(input.into());
        assert!(refusal.validate().is_err());
    }
    assert!(WindowMeter::new("requests", 1e-29, 1.0, None).is_err());
    let runtime = Runtime::memory(ReplayMode::Record);
    assert!(runtime
        .run_with_meters("tiny", None, BTreeMap::from([("usd".into(), 1e-29)]), 0)
        .is_err());
    assert!(ModelPrice::new("1e-28", "0").is_ok());
}

fn catalog(rate: Value) -> Value {
    json!({"models":[{"model_id":"test", "provider":"test", "window_nominal":100,
        "pricing":{"input":rate,"output":0}}]})
}
#[test]
fn profile_prices_check_original_number_before_float_deserialization() {
    for text in ["1e-29", "1e-1000", "1.23456789012345678901234567891"] {
        let value: Value = serde_json::from_str(text).unwrap();
        assert!(
            ProfileRegistry::from_value(&catalog(value)).is_err(),
            "{text}"
        );
    }
    let mut value = catalog(json!(1));
    value["models"][0]["pricing_tiers"] =
        json!([{"min_input_tokens":5,"pricing":{"input":1e-29,"output":0}}]);
    assert!(ProfileRegistry::from_value(&value).is_err());
    let pricing = Pricing {
        input: 1e-29,
        output: 0.0,
        cache_read: 0.0,
        cache_write: 0.0,
        currency: "USD".into(),
        as_of: None,
    };
    assert!(pricing.validate().is_err());
}

#[test]
fn nonzero_costs_cannot_silently_underflow_to_zero() {
    let meter = CostMeter::new(BTreeMap::from([(
        "test".into(),
        ModelPrice::new("1e-28", "0").unwrap(),
    )]));
    let usage = |count| json!({"usage":{"input_tokens":count,"output_tokens":0}});
    assert!(meter
        .charge_decimal(NodeKind::ModelCall, &json!({"model":"test"}), &usage(1))
        .is_err());
    assert_eq!(
        meter
            .charge_decimal(NodeKind::ModelCall, &json!({"model":"test"}), &usage(0))
            .unwrap(),
        Decimal::ZERO
    );
    assert_eq!(
        meter
            .charge_decimal(
                NodeKind::ModelCall,
                &json!({"model":"test"}),
                &usage(1_000_000)
            )
            .unwrap(),
        parse_decimal_exact("1e-28").unwrap()
    );
    let registry = ProfileRegistry::from_value(&catalog(json!(1e-28))).unwrap();
    assert!(quote_estimate(&registry, "test", 1, 0, false).is_err());
    assert_eq!(
        quote_estimate(&registry, "test", 0, 0, false)
            .unwrap()
            .total,
        Decimal::ZERO
    );
    // A nonzero rounded result must fail too: 6e-29 cannot become 1e-28.
    let rounded = CostMeter::new(BTreeMap::from([(
        "test".into(),
        ModelPrice::new("6e-23", "0").unwrap(),
    )]));
    assert!(rounded
        .charge_decimal(NodeKind::ModelCall, &json!({"model":"test"}), &usage(1))
        .is_err());
    let registry = ProfileRegistry::from_value(&catalog(json!(6e-23))).unwrap();
    assert!(quote_estimate(&registry, "test", 1, 0, false).is_err());
}

#[test]
fn imported_recording_charge_does_not_underflow_before_validation() {
    for text in ["1e-1000", "1e-29", "1.23456789012345678901234567891"] {
        let mut store = MemoryStore::new();
        let amount: Value = serde_json::from_str(text).unwrap();
        let root = Node::make(
            NodeKind::Root,
            None,
            0,
            json!({"run":"imported-tiny"}),
            None,
            json!({"charges":{"usd":amount}}),
        )
        .unwrap();
        store.put(root).unwrap();
        let runtime = Runtime::new(store, ReplayMode::Replay);
        let result = runtime
            .run("imported-tiny", None, 0)
            .and_then(|run| run.report());
        assert!(result.is_err(), "{text}");
    }
}

#[test]
fn exact_costs_match_wide_python_decimal_reference_or_reject_without_rounding() {
    let fixtures: Value = serde_json::from_str(include_str!("pypi160_decimal_wire.json")).unwrap();
    for row in fixtures["costs"].as_array().unwrap() {
        let price = ModelPrice::new(
            row["input_rate"].as_str().unwrap(),
            row["output_rate"].as_str().unwrap(),
        )
        .unwrap();
        let meter = CostMeter::new(BTreeMap::from([("test".into(), price)]));
        let result = meter.charge_decimal(
            NodeKind::ModelCall,
            &json!({"model":"test"}),
            &json!({"usage":{"input_tokens":row["inputs"],"output_tokens":row["outputs"]}}),
        );
        if row["representable"] == json!(true) {
            assert_eq!(
                result.unwrap(),
                parse_decimal_exact(row["exact"].as_str().unwrap()).unwrap(),
                "{row}"
            );
        } else {
            assert!(result.is_err(), "{row}: {result:?}");
        }
    }
}

#[test]
fn public_exact_addition_and_subtraction_never_discard_low_digits() {
    let tiny = parse_decimal_exact("1e-28").unwrap();
    assert_eq!(checked_decimal_add(Decimal::MAX, tiny), None);
    assert_eq!(checked_decimal_subtract(Decimal::MAX, tiny), None);
    assert_eq!(
        checked_decimal_add(Decimal::MAX, -Decimal::MAX),
        Some(Decimal::ZERO)
    );
    assert_eq!(
        checked_decimal_subtract(tiny, Decimal::ONE),
        Some(parse_decimal_exact("-0.9999999999999999999999999999").unwrap())
    );
}
