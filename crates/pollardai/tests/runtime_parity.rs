use pollardai::*;
use std::{
    cell::Cell,
    collections::BTreeMap,
    rc::Rc,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

fn usage(n: u64) -> Value {
    json!({"text":"first","usage":{"input_tokens":n,"output_tokens":0}})
}

#[test]
fn default_duplicates_execute_preserve_original_and_append_conflict() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("duplicates", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let first = run
        .model_call(json!({"model":"mock"}), CallOptions::default(), |_| {
            Ok(usage(5))
        })
        .unwrap();
    run.rollback(&root).unwrap();
    let mut invoked = false;
    let second = run
        .model_call(json!({"model":"mock"}), CallOptions::default(), |_| {
            invoked = true;
            Ok(usage(9))
        })
        .unwrap();
    assert!(invoked);
    assert_eq!(first.id, second.id);
    assert_eq!(first.result, second.result);
    assert_eq!(
        second.meta["result_conflicts"][0]["result"]["usage"]["input_tokens"],
        json!(9)
    );
    assert_eq!(run.report().unwrap().spent["tokens"], 5.0);
    assert!(run.report().unwrap().avoided.is_empty());
}

#[test]
fn fallback_and_no_estimate_match_python_160() {
    for estimate in [None, Some(10)] {
        let runtime = Runtime::memory(ReplayMode::Record);
        let mut run = runtime
            .run(
                "fallback",
                Some(Budget {
                    tokens: Some(100),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        let node = run
            .model_call(
                json!({}),
                CallOptions {
                    estimated_tokens: estimate,
                    ..Default::default()
                },
                |_| Ok(json!({"text":"ok"})),
            )
            .unwrap();
        assert_eq!(run.spent().unwrap().tokens, estimate.unwrap_or(0));
        assert_eq!(
            node.meta.get("accounting_fallbacks").is_some(),
            estimate.is_some()
        );
    }
}

#[test]
fn resumes_pruning_and_rollback_steps_are_deterministic() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("resume", None, 0).unwrap();
    let first = run.note(json!({"n":1}), 0).unwrap();
    let second = run.note(json!({"n":2}), 0).unwrap();
    assert_eq!(
        runtime.resume("resume", None, 0).unwrap().cursor_id(),
        second.id
    );
    run.prune().unwrap();
    assert_eq!(
        runtime.resume("resume", None, 0).unwrap().cursor_id(),
        first.id
    );
    assert_eq!(run.rollback_steps(1).unwrap().id, first.id);
    assert_eq!(run.rollback_steps(500).unwrap().id, run.root_id());
    assert!(runtime.resume("missing", None, 0).is_err());
}

#[test]
fn dry_run_only_skips_registered_side_effects() {
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let count = calls.clone();
    let spec = ActionSpec::new(
        "send",
        "1",
        "",
        json!({"type":"object"}),
        true,
        Some(Arc::new(move |_| {
            count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(json!({}))
        })),
    )
    .unwrap();
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_registry(Registry::new(vec![spec]).unwrap())
        .with_dry_run(true);
    let mut run = runtime.run("dry", None, 0).unwrap();
    let node = run
        .registered_tool_call("send", None, json!({}), CallOptions::default())
        .unwrap();
    assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert!(node.result.is_none());
    assert_eq!(node.meta["dry_run"], json!(true));
    assert_eq!(run.spent().unwrap().steps, 1);
    let mut invoked = false;
    run.model_call(json!({}), CallOptions::default(), |_| {
        invoked = true;
        Ok(json!({}))
    })
    .unwrap();
    assert!(invoked);
}

struct UnitMeter;
impl Meter for UnitMeter {
    fn name(&self) -> &str {
        "units"
    }
    fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
        Ok(Some(2.5))
    }
    fn charge(&self, _: NodeKind, _: &Value, _: &Value, _: &Value) -> Result<f64> {
        Ok(3.0)
    }
}
#[test]
fn custom_meter_scopes_overshoot_and_replay_avoidance() {
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(UnitMeter);
    let mut run = runtime
        .run_with_meters("units", None, BTreeMap::from([("units".into(), 2.5)]), 0)
        .unwrap();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
        .unwrap();
    assert_eq!(run.report().unwrap().spent["units"], 3.0);
    assert!(
        matches!(run.model_call(json!({"next":true}), CallOptions::default(), |_| panic!("must gate")), Err(Error::BudgetExceeded{meter,..}) if meter=="units")
    );
    let replay =
        Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay).with_meter(UnitMeter);
    let mut reused = replay.run("units", None, 0).unwrap();
    reused
        .model_call(json!({}), CallOptions::default(), |_| panic!("replay"))
        .unwrap();
    assert_eq!(reused.report().unwrap().avoided["units"], 3.0);
}

#[test]
fn shared_cached_accounting_tracks_siblings_and_pending_settlement() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "cache",
            Some(Budget {
                steps: Some(100),
                tokens: Some(100),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(run.report().unwrap().spent.is_empty());
    for n in 0..20 {
        let mut branch = run.branch(n, None).unwrap();
        branch
            .model_call(
                json!({}),
                CallOptions {
                    estimated_tokens: Some(3),
                    ..Default::default()
                },
                |_| Ok(usage(1)),
            )
            .unwrap();
        assert_eq!(
            run.spent().unwrap(),
            Charges {
                steps: n + 1,
                tokens: n + 1
            }
        );
    }
    let mut branch = run
        .branch(
            21,
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
        )
        .unwrap();
    assert_eq!(branch.spent().unwrap().steps, 20);
    branch
        .model_call(json!({}), CallOptions::default(), |_| Ok(usage(2)))
        .unwrap();
    assert!(matches!(
        branch.model_call(json!({}), CallOptions::default(), |_| panic!(
            "branch scope"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    assert_eq!(run.spent().unwrap().tokens, 22);
}

#[test]
fn callback_failures_are_inspectable_and_do_not_lose_recordings() {
    let count = Rc::new(Cell::new(0));
    let seen = count.clone();
    let runtime = Runtime::memory(ReplayMode::Record).with_on_node(Rc::new(move |_| {
        seen.set(seen.get() + 1);
        Err(Error::Handler("observer".into()))
    }));
    let mut run = runtime.run("observer", None, 0).unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| Ok(usage(0)))
        .unwrap();
    assert_eq!(count.get(), 2);
    assert_eq!(runtime.callback_errors().len(), 2);
    assert!(verify(&*runtime.store(), &node.id).ok);
}

#[derive(Clone)]
struct CountedStore {
    inner: Rc<std::cell::RefCell<MemoryStore>>,
    reads: Rc<Cell<usize>>,
}
impl Store for CountedStore {
    fn put(&mut self, node: Node) -> Result<()> {
        self.inner.borrow_mut().put(node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.reads.set(self.reads.get() + 1);
        self.inner.borrow().get(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.inner.borrow().exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.inner.borrow().children(id)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.inner.borrow_mut().update_meta(id, patch)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.inner.borrow().roots()
    }
}
impl RecordingStore for CountedStore {
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.inner.borrow_mut().finalize(node)
    }
    fn revision(&self) -> Option<u64> {
        self.inner.borrow().revision()
    }
    fn trusted_immutable_identity(&self) -> bool {
        true
    }
}
#[test]
fn incremental_accounting_has_linear_reads_and_invalidates_external_changes() {
    let store = CountedStore {
        inner: Rc::new(std::cell::RefCell::new(MemoryStore::new())),
        reads: Rc::new(Cell::new(0)),
    };
    let raw = store.inner.clone();
    let reads = store.reads.clone();
    let runtime = Runtime::new(store, ReplayMode::Record);
    let mut run = runtime
        .run(
            "indexed",
            Some(Budget {
                steps: Some(1000),
                tokens: Some(2000),
                depth: Some(1000),
            }),
            0,
        )
        .unwrap();
    let mut first = String::new();
    for n in 0..200 {
        let node = run
            .model_call(
                json!({"n":n}),
                CallOptions {
                    estimated_tokens: Some(3),
                    ..Default::default()
                },
                |_| Ok(usage(2)),
            )
            .unwrap();
        if first.is_empty() {
            first = node.id;
        }
        assert_eq!(run.spent().unwrap().tokens, (n + 1) * 2);
    }
    assert!(
        reads.get() < 3000,
        "{} reads must stay linear instead of repeatedly scanning ancestry/tree",
        reads.get()
    );
    raw.borrow_mut()
        .update_meta(&first, json!({"charges":{"steps":1,"tokens":20}}))
        .unwrap();
    assert_eq!(
        run.spent().unwrap().tokens,
        418,
        "external revisions must invalidate aggregate cache"
    );
    run.model_call(json!({"after":true}), CallOptions::default(), |_| {
        Ok(usage(2))
    })
    .unwrap();
    assert_eq!(run.spent().unwrap().tokens, 420);
}

#[test]
fn window_meter_gates_across_resumes_and_never_dispatches_refused_call() {
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_meter(WindowMeter::new("requests", 2.0, 60.0, None).unwrap());
    let mut run = runtime.run("window", None, 0).unwrap();
    run.model_call(json!({"n":1}), CallOptions::default(), |_| Ok(usage(0)))
        .unwrap();
    run.model_call(json!({"n":2}), CallOptions::default(), |_| Ok(usage(0)))
        .unwrap();
    let mut resumed = runtime.resume("window", None, 0).unwrap();
    assert!(
        matches!(resumed.model_call(json!({"n":3}),CallOptions::default(), |_| panic!("window limit")),Err(Error::BudgetExceeded{meter,..}) if meter=="requests")
    );
    assert_eq!(resumed.cursor().unwrap().payload["reason"], json!("window"));
    assert_eq!(
        resumed.cursor().unwrap().payload["window_seconds"],
        json!(60)
    );
}

#[test]
fn invalid_custom_estimate_stops_before_pending_marker_or_handler() {
    struct Invalid;
    impl Meter for Invalid {
        fn name(&self) -> &str {
            "bad"
        }
        fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
            Ok(Some(f64::NAN))
        }
        fn charge(&self, _: NodeKind, _: &Value, _: &Value, _: &Value) -> Result<f64> {
            Ok(0.0)
        }
    }
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(Invalid);
    let mut run = runtime.run("invalid-meter", None, 0).unwrap();
    let root = run.root_id().to_owned();
    assert!(run
        .model_call(json!({}), CallOptions::default(), |_| panic!(
            "invalid estimate"
        ))
        .is_err());
    assert_eq!(run.cursor_id(), root);
    assert!(run.report().unwrap().spent.is_empty());
}

#[test]
fn live_revalidation_preserves_recording_and_returns_original_cursor() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let contract = ReplayContract::new("mock").unwrap();
    let payload = contract
        .bind(json!({"model":"mock","prompt":"private"}))
        .unwrap();
    let mut run = runtime.run("revalidation", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let recorded = run
        .model_call(payload.clone(), CallOptions::default(), |_| Ok(usage(2)))
        .unwrap();
    run.rollback(&root).unwrap();
    let report = run
        .revalidate_model_call(
            payload.clone(),
            &contract,
            RevalidationOptions::new("observe-1"),
            |received| {
                assert_eq!(
                    received, payload,
                    "provider receives original live input without observation identity marker"
                );
                Ok(usage(4))
            },
        )
        .unwrap();
    assert!(report.matched);
    assert!(!report.exact_match);
    assert_eq!(run.cursor_id(), recorded.id);
    assert_eq!(
        runtime.store().get(&recorded.id).unwrap().result,
        recorded.result
    );
    assert_eq!(run.spent().unwrap().tokens, 6);
    let evidence = runtime.store().get(&report.evidence_node_id).unwrap();
    assert!(!evidence.payload.to_string().contains("private"));
    assert_eq!(evidence.payload["comparison"]["matched"], json!(true));
    assert!(verify(&*runtime.store(), &evidence.id).ok);
    run.rollback(&root).unwrap();
    assert!(matches!(
        run.revalidate_model_call(
            payload,
            &contract,
            RevalidationOptions::new("observe-1"),
            |_| panic!("observation reuse")
        ),
        Err(Error::Integrity(_))
    ));
}

#[test]
fn revalidation_comparator_failure_retains_safe_evidence_and_restores_cursor() {
    struct Broken;
    impl RevalidationComparator for Broken {
        fn name(&self) -> &str {
            "broken/v1"
        }
        fn compare(&self, _: &Value, _: &Value) -> Result<RevalidationComparison> {
            Err(Error::Handler("private secret".into()))
        }
    }
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("compare-failure", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let recorded = run
        .model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
        .unwrap();
    run.rollback(&root).unwrap();
    let error = run
        .revalidate_model_call_with_comparator(
            json!({}),
            &ReplayContract::new("mock").unwrap(),
            RevalidationOptions::new("obs-error"),
            &Broken,
            |_| Ok(usage(2)),
        )
        .unwrap_err();
    assert!(matches!(error, Error::Handler(_)));
    assert_eq!(run.cursor_id(), recorded.id);
    let nodes = runtime.store().walk(&root).unwrap();
    let evidence = nodes
        .iter()
        .find(|node| node.payload["event"] == json!("model_revalidation_comparison_failed"))
        .unwrap();
    assert!(!evidence.payload.to_string().contains("private secret"));
    assert_eq!(run.spent().unwrap().tokens, 3);
}

#[test]
fn duplicate_dispatches_also_consume_window_capacity() {
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_meter(WindowMeter::new("requests", 2.0, 60.0, None).unwrap());
    let mut run = runtime.run("duplicate-window", None, 0).unwrap();
    let root = run.root_id().to_owned();
    for _ in 0..2 {
        run.model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
            .unwrap();
        run.rollback(&root).unwrap();
    }
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!(
            "window must count every dispatch"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    assert_eq!(
        run.spent().unwrap().steps,
        1,
        "immutable identity report keeps original recording charges"
    );
}

#[test]
fn decimal_meter_budget_does_not_refuse_exact_boundary() {
    struct Fraction;
    impl Meter for Fraction {
        fn name(&self) -> &str {
            "usd"
        }
        fn estimate(&self, _: NodeKind, payload: &Value) -> Result<Option<f64>> {
            Ok(payload["amount"]
                .as_str()
                .and_then(|amount| amount.parse().ok()))
        }
        fn charge(&self, _: NodeKind, payload: &Value, _: &Value, _: &Value) -> Result<f64> {
            Ok(payload["amount"].as_str().unwrap().parse().unwrap())
        }
    }
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(Fraction);
    let mut run = runtime
        .run_with_meters("decimal", None, BTreeMap::from([("usd".into(), 0.3)]), 0)
        .unwrap();
    for amount in ["0.1", "0.2"] {
        run.model_call(json!({"amount":amount}), CallOptions::default(), |_| {
            Ok(usage(0))
        })
        .unwrap();
    }
    assert_eq!(run.report().unwrap().spent["usd"], 0.3);
    assert!(
        matches!(run.model_call(json!({"amount":"0.1"}),CallOptions::default(), |_| panic!("exhausted")),Err(Error::BudgetExceeded{meter,..}) if meter=="usd")
    );
}

#[test]
fn revalidation_estimators_receive_original_live_payload() {
    struct Checked;
    impl Meter for Checked {
        fn name(&self) -> &str {
            "checked"
        }
        fn estimate(&self, _: NodeKind, payload: &Value) -> Result<Option<f64>> {
            assert!(payload.pointer("/_pollard/revalidation").is_none());
            Ok(Some(1.0))
        }
        fn charge(&self, _: NodeKind, payload: &Value, _: &Value, _: &Value) -> Result<f64> {
            assert!(payload.pointer("/_pollard/revalidation").is_none());
            Ok(1.0)
        }
    }
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(Checked);
    let mut run = runtime.run("meter-revalidation", None, 0).unwrap();
    let root = run.root_id().to_owned();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(usage(0)))
        .unwrap();
    run.rollback(&root).unwrap();
    run.revalidate_model_call(
        json!({}),
        &ReplayContract::new("mock").unwrap(),
        RevalidationOptions::new("meter"),
        |_| Ok(usage(0)),
    )
    .unwrap();
}

#[test]
fn ordinary_failure_releases_but_unknown_failure_preserves_charges() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime
        .run(
            "failures",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let root = run.root_id().to_owned();
    assert!(run
        .model_call(json!({}), CallOptions::default(), |_| Err(Error::Handler(
            "known".into()
        )))
        .is_err());
    assert_eq!(run.cursor_id(), root);
    assert_eq!(run.spent().unwrap().steps, 0);
    let error = run
        .model_call(json!({}), CallOptions::default(), |_| {
            Err(Error::OutcomeUnknown(Box::new(Error::Handler(
                "unknown".into(),
            ))))
        })
        .unwrap_err();
    assert!(error.is_post_dispatch_outcome_unknown());
    assert_eq!(run.spent().unwrap().steps, 1);
}

fn sqlite_runtime_path(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "pollard-runtime-{label}-{}-{}-{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn sqlite_runtime_arbitrates_concurrent_dispatches_and_shared_window() {
    for window in [false, true] {
        let path = sqlite_runtime_path(if window { "window" } else { "budget" });
        drop(SQLiteStore::open(&path).unwrap());
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let executions = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let workers: Vec<_> = (0..8)
            .map(|worker| {
                let path = path.clone();
                let barrier = barrier.clone();
                let executions = executions.clone();
                std::thread::spawn(move || {
                    let mut runtime =
                        Runtime::new(SQLiteStore::open(path).unwrap(), ReplayMode::Record);
                    if window {
                        runtime = runtime
                            .with_meter(WindowMeter::new("requests", 3.0, 60.0, None).unwrap());
                    }
                    let budget = if window {
                        None
                    } else {
                        Some(Budget {
                            steps: Some(3),
                            ..Default::default()
                        })
                    };
                    let mut run = runtime.run("shared", budget, 0).unwrap();
                    barrier.wait();
                    let result =
                        run.model_call(json!({"worker":worker}), CallOptions::default(), |_| {
                            executions.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                            std::thread::sleep(std::time::Duration::from_millis(15));
                            Ok(usage(1))
                        });
                    assert!(
                        result.is_ok() || matches!(result, Err(Error::BudgetExceeded { .. })),
                        "{result:?}"
                    );
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(executions.load(std::sync::atomic::Ordering::SeqCst), 3);
        let _ = std::fs::remove_file(path);
    }
}

#[test]
fn sqlite_runtime_renews_live_lease_and_releases_known_errors() {
    let path = sqlite_runtime_path("lease");
    let runtime = Runtime::new(SQLiteStore::open(&path).unwrap(), ReplayMode::Record)
        .with_reservation_lease_seconds(1.0)
        .unwrap();
    let mut run = runtime
        .run(
            "lease",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let root = run.root_id().to_owned();
    assert!(run
        .model_call(json!({"fails":true}), CallOptions::default(), |_| Err(
            Error::Handler("before dispatch".into())
        ))
        .is_err());
    assert_eq!(run.cursor_id(), root);
    let query_path = path.clone();
    run.model_call(json!({"long":true}), CallOptions::default(), |_| {
        let mut independent = SQLiteStore::open(&query_path).unwrap();
        let pending = independent
            .walk(&root)
            .unwrap()
            .into_iter()
            .find(|node| node.meta["state"] == json!("pending"))
            .unwrap();
        let reservation = pending.meta["reservation_id"].as_str().unwrap().to_owned();
        std::thread::sleep(std::time::Duration::from_millis(1500));
        assert!(
            independent.renew(&reservation, 1.0).unwrap(),
            "runtime heartbeat must renew leases during slow calls"
        );
        Ok(usage(1))
    })
    .unwrap();
    assert_eq!(run.spent().unwrap().steps, 1);
    drop(run);
    drop(runtime);
    let _ = std::fs::remove_file(path);
}

#[test]
fn expected_meter_refusal_is_audited_before_dispatch_and_revalidates_metadata() {
    struct Refusing(bool);
    impl Meter for Refusing {
        fn name(&self) -> &str {
            "quota"
        }
        fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
            let mut refusal = MeterPrecheckRefusal::new("quota_unavailable")?;
            refusal.detail = "quota must be restored".into();
            refusal.audit_meta = if self.0 {
                json!({"charges":{"steps":999}})
            } else {
                json!({"provider":"mock"})
            };
            refusal.requested = Some("1".into());
            refusal.remaining = Some("0".into());
            Err(refusal.into())
        }
        fn charge(&self, _: NodeKind, _: &Value, _: &Value, _: &Value) -> Result<f64> {
            Ok(0.0)
        }
    }
    for invalid in [false, true] {
        let runtime = Runtime::memory(ReplayMode::Record).with_meter(Refusing(invalid));
        let mut run = runtime.run("meter-refusal", None, 0).unwrap();
        let root = run.root_id().to_owned();
        let error = run
            .model_call(json!({"private":"input"}), CallOptions::default(), |_| {
                panic!("meter refusal")
            })
            .unwrap_err();
        if invalid {
            assert!(matches!(error, Error::Invalid(_)));
            assert_eq!(run.cursor_id(), root);
        } else {
            assert!(matches!(error,Error::BudgetExceeded{meter,..} if meter=="quota"));
            let node = run.cursor().unwrap();
            assert_eq!(node.payload["reason"], json!("quota_unavailable"));
            assert_eq!(node.meta["provider"], json!("mock"));
            assert!(!node.payload.to_string().contains("private"));
        }
    }
}

#[test]
fn explicit_meter_list_replaces_defaults_like_python() {
    let runtime = Runtime::memory(ReplayMode::Record).with_meters(vec![Rc::new(StepMeter)]);
    let mut run = runtime.run("selected-meters", None, 0).unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| Ok(usage(99)))
        .unwrap();
    assert_eq!(node.meta["charges"], json!({"steps":1}));
    assert_eq!(
        run.report().unwrap().spent,
        BTreeMap::from([("steps".into(), 1.0)])
    );
}

#[derive(Clone)]
struct FailCompletion(MemoryStore);
impl Store for FailCompletion {
    fn put(&mut self, node: Node) -> Result<()> {
        self.0.put(node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.0.get(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.0.exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.0.children(id)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.0.update_meta(id, patch)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.0.roots()
    }
}
impl RecordingStore for FailCompletion {
    fn finalize(&mut self, node: Node) -> Result<()> {
        if node.result.is_some() {
            Err(Error::Integrity("write failed after dispatch".into()))
        } else {
            self.0.finalize(node)
        }
    }
}
#[test]
fn post_result_storage_failure_settles_conservative_record_on_drop() {
    let runtime = Runtime::new(FailCompletion(MemoryStore::new()), ReplayMode::Record);
    let mut run = runtime.run("storage-failure", None, 0).unwrap();
    let error = run
        .model_call(
            json!({}),
            CallOptions {
                estimated_tokens: Some(7),
                ..Default::default()
            },
            |_| Ok(usage(2)),
        )
        .unwrap_err();
    assert!(error.is_post_dispatch_outcome_unknown());
    assert!(matches!(error,Error::OutcomeUnknown(inner) if matches!(*inner,Error::Integrity(_))));
    assert_eq!(
        run.spent().unwrap(),
        Charges {
            steps: 1,
            tokens: 7
        }
    );
    assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
    assert_eq!(
        run.cursor().unwrap().meta["error"],
        json!("outcome_unknown")
    );
}

#[test]
fn async_registered_actions_gate_and_confirm_before_constructing_future() {
    futures::executor::block_on(async {
        let constructions = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counter = constructions.clone();
        let action = ActionSpec::new("send", "1", "", json!({"type":"object"}), true, None)
            .unwrap()
            .with_async_handler(Arc::new(move |args| {
                counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Box::pin(async move {
                    assert!(args.is_object());
                    Ok(usage(1))
                })
            }));
        let registry = Registry::new(vec![action]).unwrap();
        let denied = Runtime::memory(ReplayMode::Record)
            .with_registry(registry.clone())
            .with_policy(Arc::new(|_| Decision::Deny));
        let mut run = denied.run("deny-async", None, 0).unwrap();
        assert!(matches!(
            run.registered_tool_call_async("send", None, json!({}), CallOptions::default())
                .await,
            Err(Error::PolicyViolation { .. })
        ));
        let runtime = Runtime::memory(ReplayMode::Record)
            .with_registry(registry.clone())
            .with_policy(Arc::new(|_| Decision::Confirm));
        let mut run = runtime.run("confirm-async", None, 0).unwrap();
        let token = match run
            .registered_tool_call_async("send", None, json!({}), CallOptions::default())
            .await
            .unwrap_err()
        {
            Error::ConfirmationRequired { token } => token,
            error => panic!("{error}"),
        };
        assert_eq!(constructions.load(std::sync::atomic::Ordering::SeqCst), 0);
        run.confirm_async(&token).await.unwrap();
        assert!(run.confirm_async(&token).await.is_err());
        assert_eq!(constructions.load(std::sync::atomic::Ordering::SeqCst), 1);
        let mut no_budget = runtime
            .run(
                "no-budget-async",
                Some(Budget {
                    steps: Some(0),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        let token = match no_budget
            .registered_tool_call_async("send", None, json!({}), CallOptions::default())
            .await
            .unwrap_err()
        {
            Error::ConfirmationRequired { token } => token,
            error => panic!("{error}"),
        };
        assert!(matches!(
            no_budget.confirm_async(&token).await,
            Err(Error::BudgetExceeded { .. })
        ));
        assert_eq!(constructions.load(std::sync::atomic::Ordering::SeqCst), 1);
        let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay)
            .with_registry(registry);
        replay
            .run("confirm-async", None, 0)
            .unwrap()
            .registered_tool_call_async("send", None, json!({}), CallOptions::default())
            .await
            .unwrap();
        assert_eq!(constructions.load(std::sync::atomic::Ordering::SeqCst), 1);
    });
}

#[test]
fn cancelled_async_registered_action_settles_sqlite_budget() {
    use std::future::Future;
    let path = sqlite_runtime_path("async-tool-cancel");
    let action = ActionSpec::new("send", "1", "", json!({"type":"object"}), true, None)
        .unwrap()
        .with_async_handler(Arc::new(|_| {
            Box::pin(futures::future::pending::<Result<Value>>())
        }));
    let runtime = Runtime::new(SQLiteStore::open(&path).unwrap(), ReplayMode::Record)
        .with_registry(Registry::new(vec![action]).unwrap());
    let mut run = runtime
        .run(
            "cancel-tool",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let mut call = Box::pin(run.registered_tool_call_async(
        "send",
        None,
        json!({}),
        CallOptions {
            estimated_tokens: Some(4),
            ..Default::default()
        },
    ));
    let waker = futures::task::noop_waker();
    assert!(call
        .as_mut()
        .poll(&mut std::task::Context::from_waker(&waker))
        .is_pending());
    drop(call);
    assert_eq!(
        run.spent().unwrap(),
        Charges {
            steps: 1,
            tokens: 4
        }
    );
    assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
    let mut next = runtime
        .run(
            "cancel-tool",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        futures::executor::block_on(next.registered_tool_call_async(
            "send",
            None,
            json!({}),
            CallOptions {
                attempt: 1,
                ..Default::default()
            }
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    drop(next);
    drop(run);
    drop(runtime);
    let _ = std::fs::remove_file(path);
}

#[test]
fn async_revalidation_records_separate_observation_and_checks_budget_before_future() {
    futures::executor::block_on(async {
        let runtime = Runtime::memory(ReplayMode::Record);
        let contract = ReplayContract::new("mock").unwrap();
        let mut run = runtime
            .run(
                "async-revalidation",
                Some(Budget {
                    steps: Some(2),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        let root = run.root_id().to_owned();
        let recorded = run
            .model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
            .unwrap();
        run.rollback(&root).unwrap();
        let report = run
            .revalidate_model_call_async(
                json!({}),
                &contract,
                RevalidationOptions::new("async-observation"),
                |_| async { Ok(usage(2)) },
            )
            .await
            .unwrap();
        assert!(report.matched);
        assert!(!report.exact_match);
        assert_eq!(run.cursor_id(), recorded.id);
        assert_eq!(
            runtime.store().get(&recorded.id).unwrap().result,
            recorded.result
        );
        assert_eq!(run.spent().unwrap().steps, 2);
        run.rollback(&root).unwrap();
        let constructed = Cell::new(false);
        let result = run
            .revalidate_model_call_async(
                json!({}),
                &contract,
                RevalidationOptions::new("blocked-observation"),
                |_| {
                    constructed.set(true);
                    async { Ok(usage(0)) }
                },
            )
            .await;
        assert!(matches!(result, Err(Error::BudgetExceeded { .. })));
        assert!(!constructed.get());
    });
}

#[test]
fn async_revalidation_comparison_failure_keeps_original_and_safe_evidence() {
    struct Broken;
    impl RevalidationComparator for Broken {
        fn name(&self) -> &str {
            "async-broken/v1"
        }
        fn compare(&self, _: &Value, _: &Value) -> Result<RevalidationComparison> {
            Err(Error::Handler("sensitive-comparator-error".into()))
        }
    }
    futures::executor::block_on(async {
        let runtime = Runtime::memory(ReplayMode::Record);
        let mut run = runtime.run("async-comparison-error", None, 0).unwrap();
        let root = run.root_id().to_owned();
        let recorded = run
            .model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
            .unwrap();
        run.rollback(&root).unwrap();
        let error = run
            .revalidate_model_call_async_with_comparator(
                json!({}),
                &ReplayContract::new("mock").unwrap(),
                RevalidationOptions::new("async-error"),
                &Broken,
                |_| async { Ok(usage(2)) },
            )
            .await
            .unwrap_err();
        assert!(matches!(error, Error::Handler(_)));
        assert_eq!(run.cursor_id(), recorded.id);
        let nodes = runtime.store().walk(&root).unwrap();
        let evidence = nodes
            .iter()
            .find(|node| node.payload["event"] == json!("model_revalidation_comparison_failed"))
            .unwrap();
        assert!(!evidence
            .payload
            .to_string()
            .contains("sensitive-comparator-error"));
        assert_eq!(
            runtime.store().get(&recorded.id).unwrap().result,
            recorded.result
        );
    });
}

#[test]
fn cancelled_duplicate_dispatch_consumes_local_window_capacity() {
    use std::future::Future;
    let runtime = Runtime::memory(ReplayMode::Record)
        .with_meter(WindowMeter::new("requests", 2.0, 60.0, None).unwrap());
    let mut run = runtime.run("cancel-duplicate-window", None, 0).unwrap();
    let root = run.root_id().to_owned();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(usage(1)))
        .unwrap();
    run.rollback(&root).unwrap();
    let mut future = Box::pin(run.amodel_call(json!({}), CallOptions::default(), |_| {
        futures::future::pending::<Result<Value>>()
    }));
    let waker = futures::task::noop_waker();
    assert!(future
        .as_mut()
        .poll(&mut std::task::Context::from_waker(&waker))
        .is_pending());
    drop(future);
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!(
            "window credits cannot be refunded"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}
