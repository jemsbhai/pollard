use pollardai::*;

fn fixture() -> Value {
    serde_json::from_str(include_str!("pypi160_identity_registry.json")).unwrap()
}

#[test]
fn exact_pypi_160_integer_unicode_hashing_and_redaction_vectors() {
    let fixtures = fixture();
    assert_eq!(fixtures["provenance"]["version"], "1.6.0");
    for case in fixtures["identity"].as_array().unwrap() {
        let value = &case["value"];
        assert_eq!(
            String::from_utf8(canonical_bytes(value).unwrap()).unwrap(),
            case["canonical_text"]
        );
        assert_eq!(digest_payload(value).unwrap(), case["digest"]);
        assert_eq!(
            node_id("note", None, u64::MAX, value).unwrap(),
            case["node_id"]
        );
        assert_eq!(redact(value, Some("fixture")).unwrap(), case["redacted"]);
    }
}

#[test]
fn exact_pypi_160_native_result_float_text_and_digests() {
    for case in fixture()["results"].as_array().unwrap() {
        let mut value = case["value"].clone();
        if let Value::Number(number) = &value {
            if number.to_string().contains(['.', 'e', 'E']) {
                // Exercise the Rust native float path, not just imported JSON spelling.
                value = json!(number.as_f64().unwrap());
            }
        }
        let (text, digest) = result_text_and_digest(&value).unwrap();
        assert_eq!(text, case["text"], "native value {value}");
        assert_eq!(digest, case["digest"]);
        assert_eq!(
            result_digest_from_text(case["text"].as_str().unwrap()),
            digest
        );
    }
}

#[test]
fn exact_pypi_160_redaction_marker_recognition() {
    for case in fixture()["redaction_markers"].as_array().unwrap() {
        assert_eq!(
            is_redacted(&case["value"]),
            case["is_redacted"].as_bool().unwrap()
        );
        assert_eq!(
            contains_redaction(&case["value"]),
            case["contains_redaction"].as_bool().unwrap()
        );
    }
}

#[test]
fn identity_rejects_floating_spellings_without_rounding_large_integers() {
    for text in ["1.0", "1e0", "1E+40", "-0.0", "[1, {\"nested\": 1.1}]"] {
        assert!(
            canonical_bytes(&serde_json::from_str::<Value>(text).unwrap()).is_err(),
            "{text}"
        );
    }
    for text in [
        "18446744073709551616",
        "-340282366920938463463374607431768211456",
        "9007199254740993",
    ] {
        let value: Value = serde_json::from_str(text).unwrap();
        assert_eq!(canonical_bytes(&value).unwrap(), text.as_bytes());
    }
    assert_eq!(
        canonical_bytes(&serde_json::from_str::<Value>("-0").unwrap()).unwrap(),
        b"0"
    );
    assert_ne!(
        digest_payload(&json!(true)).unwrap(),
        digest_payload(&json!(1)).unwrap()
    );
}

#[test]
fn borrowed_identity_envelope_retains_first_invalid_leaf_error_path() {
    let value: Value = serde_json::from_str(r#"{"z":[{"bad":1.25}],"a":2.5}"#).unwrap();
    assert_eq!(
        node_id("note", None, 0, &value).unwrap_err(),
        Error::Invalid("floats are not allowed in identity payloads at $.pl.z[0].bad".into())
    );
}

#[test]
fn result_serialization_is_recursive_sorted_and_preserves_imported_text() {
    let value = json!({"z": [1e-5, -0.0, 1e16], "a": {"🙂": 1e-4}});
    let (text, digest) = result_text_and_digest(&value).unwrap();
    assert_eq!(text, "{\"a\":{\"🙂\":0.0001},\"z\":[1e-05,-0.0,1e+16]}");
    assert_eq!(digest, result_digest_from_text(&text));
    assert_ne!(
        result_digest_from_text("{\"v\":1}"),
        result_digest_from_text("{ \"v\": 1 }")
    );
    let too_large: Value = serde_json::from_str("1e999").unwrap();
    assert!(result_text_and_digest(&too_large).is_err());
}

#[test]
fn native_float_results_remain_valid_nodes_after_python_text_normalization() {
    for number in [1e-5_f64, 1e-6, 1e16, -0.0, 1e20, 1e23] {
        let node = Node::make(
            NodeKind::ModelCall,
            Some(&"a".repeat(64)),
            0,
            json!({}),
            Some(json!({"value":number})),
            json!({}),
        );
        let node = node.unwrap_or_else(|error| panic!("{number}: {error}"));
        let stored = Node::from_storage(
            node.id.clone(),
            node.parent.clone(),
            node.kind,
            node.attempt,
            &node.payload.to_string(),
            node.result_text.clone(),
            node.result_digest.clone(),
            &node.meta.to_string(),
        )
        .unwrap();
        stored.validate().unwrap();
        assert_eq!(stored.result_digest, node.result_digest);
        assert_eq!(stored.result_text, node.result_text);
        assert_eq!(
            stored.result.unwrap()["value"].as_f64().unwrap().to_bits(),
            number.to_bits()
        );
    }
}

#[test]
fn float_result_integrity_still_rejects_changed_values_types_and_signed_zero() {
    let original = Node::make(
        NodeKind::ModelCall,
        Some(&"a".repeat(64)),
        0,
        json!({}),
        Some(json!({"nested":[{"value":1e-5}],"zero":-0.0,"integer":1})),
        json!({}),
    )
    .unwrap();
    for mutation in [
        json!({"nested":[{"value":1.1e-5}],"zero":-0.0,"integer":1}),
        json!({"nested":[{"value":1e-5}],"zero":0.0,"integer":1}),
        json!({"nested":[{"value":1e-5}],"zero":-0.0,"integer":1.0}),
    ] {
        let mut tampered = original.clone();
        tampered.result = Some(mutation);
        assert!(tampered.validate().is_err());
    }
    let mut altered_text = original;
    altered_text.result_text = Some(
        altered_text
            .result_text
            .unwrap()
            .replace("1e-05", "0.00001"),
    );
    assert!(
        altered_text.validate().is_err(),
        "exact text digest must remain authoritative"
    );
}

#[test]
fn node_attempts_cover_the_full_native_unsigned_range() {
    let node = Node::make(
        NodeKind::Root,
        None,
        u64::MAX,
        json!({"run":"large-attempt"}),
        None,
        json!({}),
    )
    .unwrap();
    node.validate().unwrap();
    assert_eq!(node.expected_id().unwrap(), node.id);
    let mut store = MemoryStore::new();
    store.put(node.clone()).unwrap();
    assert_eq!(store.get(&node.id).unwrap().attempt, u64::MAX);
}

#[test]
fn nonfinite_exponent_metadata_and_imported_results_are_rejected() {
    for source in ["{\"x\":1e999}", "{\"nested\":[{\"x\":-1e999}]}"] {
        let meta: Value = serde_json::from_str(source).unwrap();
        assert!(Node::make(NodeKind::Root, None, 0, json!({}), None, meta).is_err());
        assert!(Node::from_storage(
            node_id("root", None, 0, &json!({})).unwrap(),
            None,
            NodeKind::Root,
            0,
            "{}",
            None,
            None,
            source
        )
        .is_err());
    }
    let text = "{\"x\":1e999}";
    let imported = Node::from_storage(
        node_id("model_call", Some(&"a".repeat(64)), 0, &json!({})).unwrap(),
        Some("a".repeat(64)),
        NodeKind::ModelCall,
        0,
        "{}",
        Some(text.into()),
        Some(result_digest_from_text(text)),
        "{}",
    );
    assert!(imported.is_err() || imported.unwrap().validate().is_err());
    let finite: Value = serde_json::from_str(
        "{\"large_integer\":10000000000000000000000000000000000000000,\"finite_float\":1e308}",
    )
    .unwrap();
    assert!(Node::make(NodeKind::Root, None, 0, json!({}), None, finite).is_ok());
}

#[test]
fn memory_and_reopened_sqlite_nodes_compare_equal_without_weakening_text_integrity() {
    struct Database(std::path::PathBuf);
    impl Drop for Database {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
    let database = Database(std::env::temp_dir().join(format!(
            "pollard-node-float-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        )));
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"float-roundtrip"}),
        None,
        json!({}),
    )
    .unwrap();
    let node = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"integer":1}),
        Some(json!({"values":[1e-5,1e-6,1e16,-0.0]})),
        json!({"scientific":1e-5}),
    )
    .unwrap();
    let mut memory = MemoryStore::new();
    memory.put(root.clone()).unwrap();
    memory.put(node.clone()).unwrap();
    {
        let mut sqlite = SQLiteStore::open(&database.0).unwrap();
        sqlite.put(root).unwrap();
        sqlite.put(node.clone()).unwrap();
    }
    let sqlite = SQLiteStore::open_read_only(&database.0).unwrap();
    let reopened = sqlite.get(&node.id).unwrap();
    assert_eq!(memory.get(&node.id).unwrap(), reopened);
    assert_eq!(node, reopened);
    let mut wrong = reopened.clone();
    wrong.result.as_mut().unwrap()["values"][0] = json!(1.1e-5);
    assert_ne!(node, wrong);
    let mut wrong = reopened.clone();
    wrong.meta["scientific"] = json!(0.00002);
    assert_ne!(node, wrong);
    let mut wrong = reopened;
    wrong.result_text = Some(wrong.result_text.unwrap().replace("1e-05", "0.00001"));
    wrong.result_digest = wrong.result_text.as_deref().map(result_digest_from_text);
    wrong.validate().unwrap();
    assert_ne!(
        node, wrong,
        "exact serialized result text remains part of node equality"
    );
}
