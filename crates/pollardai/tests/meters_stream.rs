use pollardai::*;
use std::{collections::BTreeMap, rc::Rc};

#[test]
fn meter_contracts_and_decimal_cost_match_python() {
    let p = json!({"model":"mock"});
    let r = json!({"usage":{"input_tokens":1_000_000,"output_tokens":500_000}});
    let meta = json!({"duration_s":0.25});
    assert_eq!(
        StepMeter.estimate(NodeKind::ModelCall, &p).unwrap(),
        Some(1.0)
    );
    assert_eq!(
        StepMeter.charge(NodeKind::Note, &p, &r, &meta).unwrap(),
        0.0
    );
    assert_eq!(
        DepthMeter
            .charge(NodeKind::ModelCall, &p, &r, &meta)
            .unwrap(),
        0.0
    );
    assert_eq!(
        WallClockMeter
            .charge(NodeKind::ModelCall, &p, &r, &meta)
            .unwrap(),
        0.25
    );
    assert_eq!(
        TokenMeter::default()
            .charge(NodeKind::ModelCall, &p, &r, &meta)
            .unwrap(),
        1_500_000.0
    );
    let cost = CostMeter::new(BTreeMap::from([(
        "mock".into(),
        ModelPrice::new("2.00", "6.00").unwrap(),
    )]));
    assert_eq!(
        cost.charge_decimal(NodeKind::ModelCall, &p, &r)
            .unwrap()
            .to_string(),
        "5.00"
    );
    assert_eq!(
        cost.charge(NodeKind::ModelCall, &p, &r, &meta).unwrap(),
        5.0
    );
    assert!(ModelPrice::new("-1", "NaN").is_err());
    let tokens = TokenMeter::new(Rc::new(|_| Ok(Some(7))), 5);
    assert_eq!(
        tokens.estimate(NodeKind::ModelCall, &p).unwrap(),
        Some(12.0)
    );
    assert_eq!(tokens.estimate(NodeKind::ToolCall, &p).unwrap(), None);
    assert!(TokenMeter::new(Rc::new(|_| Ok(Some(u64::MAX))), 1)
        .estimate(NodeKind::ModelCall, &p)
        .is_err());
    for usage in [
        json!({}),
        json!({"input_tokens":true,"output_tokens":1}),
        json!({"input_tokens":-1,"output_tokens":1}),
        json!({"input_tokens":1,"output_tokens":0.5}),
    ] {
        assert_eq!(
            TokenMeter::default()
                .charge(NodeKind::ModelCall, &p, &json!({"usage":usage}), &meta)
                .unwrap(),
            0.0
        );
    }
}

#[test]
fn windows_validate_and_delegate() {
    let requests = WindowMeter::new("requests", 5.0, 60.0, None).unwrap();
    assert_eq!(
        requests.estimate(NodeKind::ModelCall, &json!({})).unwrap(),
        Some(1.0)
    );
    assert_eq!(requests.window().unwrap().seconds, 60.0);
    assert!(WindowMeter::new("", 5.0, 60.0, None).is_err());
    assert!(WindowMeter::new("r", f64::NAN, 60.0, None).is_err());
    assert!(WindowMeter::new("r", 1.0, 0.0, None).is_err());
}

#[test]
fn window_ledger_keys_match_pypi_decimal_spelling() {
    // Generated with the SHA-pinned pollard 1.6.0 wheel's WindowMeter.ledger_key.
    let root = "a".repeat(64);
    for (limit, expected) in [
        (
            "3",
            "c54ecaacf7d679b7bc9b2d0b4d826ce92f7cee539c0bdd67cba634936148a705",
        ),
        (
            "3.0",
            "36b1d8883d430fbd56181bb78608e7ca0780a9e59612a5aa9dc5f89213a1bbf6",
        ),
        (
            "3.00",
            "3fe8477f67f7538b27a99b139af0d338859626035a873fbfd4b7f1465dc631cb",
        ),
        (
            "1E+3",
            "a55f8f9c158cec66f1eceeab820b9e26161e25984db746cb71ce8b38b5631014",
        ),
        (
            "1E-7",
            "692b41ea07a31212793094e321f80285e88a877d95780ccd595bc9eb0b7e00dc",
        ),
        (
            "0.000001",
            "0efb0beb8db90f11085405cd57b6bbe1dc4b3b9c42b9a9c4706bcab069d53463",
        ),
        (
            "0.00000100",
            "c4ae1f875e5c8b2aee7e6c533aa2dfa5c15549115f360f9ff3114122f37cd8df",
        ),
    ] {
        assert_eq!(
            WindowMeter::new_decimal("requests", limit, 60.0, None)
                .unwrap()
                .window_ledger_key(&root)
                .unwrap()
                .unwrap(),
            expected,
            "{limit}"
        );
    }
    assert_eq!(
        WindowMeter::new("requests", 3.0, 60.0, None)
            .unwrap()
            .window_ledger_key(&root)
            .unwrap(),
        WindowMeter::new_decimal("requests", "3.0", 60.0, None)
            .unwrap()
            .window_ledger_key(&root)
            .unwrap()
    );
}

#[test]
fn energy_integration_is_checked() {
    assert_eq!(
        integrate_energy(&[(0.0, 10.0), (1.0, 20.0), (2.0, 10.0)]).unwrap(),
        30.0
    );
    assert_eq!(integrate_energy(&[]).unwrap(), 0.0);
    assert!(integrate_energy(&[(1.0, 10.0), (0.0, 10.0)]).is_err());
    assert!(integrate_energy(&[(0.0, f64::INFINITY)]).is_err());
}

#[test]
fn stream_contract_merges_and_retains_snapshots() {
    let chunks = vec![
        json!({"delta":{"text":"hel","items":[1],"nested":{"x":"a"}}}),
        json!({"delta":{"text":"lo","items":[2],"nested":{"x":"b"},"usage":{"input_tokens":2}}}),
        json!({"usage":{"output_tokens":3}}),
    ];
    let mut seen = Vec::new();
    let result = consume_stream(chunks.clone().into_iter().map(Ok), true, |c| {
        seen.push(c.clone());
        Ok(())
    })
    .unwrap();
    assert_eq!(result["text"], "hello");
    assert_eq!(result["items"], json!([1, 2]));
    assert_eq!(result["nested"]["x"], "ab");
    assert_eq!(result["usage"], json!({"input_tokens":2,"output_tokens":3}));
    assert_eq!(result["chunks"], json!(chunks));
    let mut replay = Vec::new();
    reemit_chunks(&result, |c| {
        replay.push(c.clone());
        Ok(())
    })
    .unwrap();
    assert_eq!(replay, seen);
    let completed = consume_stream(
        [
            Ok(json!({"text":"old"})),
            Ok(json!({"result":{"text":"new"}})),
        ],
        false,
        |_| Ok(()),
    )
    .unwrap();
    assert_eq!(completed, json!({"text":"new"}));
    assert!(consume_stream([Ok(json!({"delta":true}))], false, |_| Ok(())).is_err());
    assert!(consume_stream([Ok(json!(42))], false, |_| Ok(())).is_err());
    assert!(
        consume_stream([Ok(json!({}))], false, |_| Err(Error::Handler(
            "observer".into()
        )))
        .is_err()
    );
}
