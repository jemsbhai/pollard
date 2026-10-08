use futures::{future::pending, task::noop_waker};
use pollardai::*;
use std::{
    cell::{Cell, RefCell},
    collections::BTreeSet,
    future::Future,
    rc::Rc,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    task::{Context, Poll},
    time::Duration,
};

#[derive(Clone, Copy, PartialEq)]
enum Fault {
    None,
    Factory,
    Start,
    Finish,
    PanicFinish,
    Readings,
    InvalidReadings,
    PanicFactory,
}
type Events = Rc<RefCell<Vec<String>>>;
struct Measuring {
    name: &'static str,
    events: Events,
    fault: Rc<Cell<Fault>>,
}
impl Measuring {
    fn new(name: &'static str, events: &Events, fault: Fault) -> Self {
        Self {
            name,
            events: events.clone(),
            fault: Rc::new(Cell::new(fault)),
        }
    }
}
impl Meter for Measuring {
    fn name(&self) -> &str {
        self.name
    }
    fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
        Ok(Some(5.0))
    }
    fn measure(&self) -> Result<Option<Box<dyn MeterMeasurement>>> {
        self.events
            .borrow_mut()
            .push(format!("{}:factory", self.name));
        match self.fault.get() {
            Fault::Factory => return Err(Error::Handler("factory failure".into())),
            Fault::PanicFactory => panic!("factory panic"),
            _ => (),
        }
        Ok(Some(Box::new(Measurement {
            name: self.name,
            events: self.events.clone(),
            fault: self.fault.get(),
        })))
    }
    fn charge(&self, _: NodeKind, _: &Value, _: &Value, meta: &Value) -> Result<f64> {
        Ok(meta[self.name].as_f64().unwrap_or(0.0))
    }
    fn charge_with_meta(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        self.events
            .borrow_mut()
            .push(format!("{}:charge", self.name));
        meta[format!("{}_audit", self.name)] = json!({"observed":meta[self.name]});
        self.charge(kind, payload, result, meta)
    }
}
struct Measurement {
    name: &'static str,
    events: Events,
    fault: Fault,
}
impl MeterMeasurement for Measurement {
    fn start(&mut self) -> Result<()> {
        self.events
            .borrow_mut()
            .push(format!("{}:start", self.name));
        if self.fault == Fault::Start {
            return Err(Error::Handler("start failure".into()));
        }
        Ok(())
    }
    fn finish(&mut self, error: Option<&Error>) -> Result<()> {
        self.events
            .borrow_mut()
            .push(format!("{}:finish:{}", self.name, error.is_some()));
        match self.fault {
            Fault::Finish => Err(Error::Handler(format!("{} finish failure", self.name))),
            Fault::PanicFinish => panic!("finish panic"),
            _ => Ok(()),
        }
    }
    fn readings(&self) -> Result<Value> {
        self.events
            .borrow_mut()
            .push(format!("{}:readings", self.name));
        match self.fault {
            Fault::Readings => Err(Error::Handler("readings failure".into())),
            Fault::InvalidReadings => Ok(json!([])),
            _ => Ok(json!({self.name:2})),
        }
    }
}

#[test]
fn lifecycle_is_ordered_and_metadata_is_available_before_mutable_charge() {
    let events = Events::default();
    let runtime = Runtime::memory(ReplayMode::Record).with_meters(vec![
        Rc::new(Measuring::new("a", &events, Fault::None)),
        Rc::new(Measuring::new("b", &events, Fault::None)),
    ]);
    let mut run = runtime.run("measurement", None, 0).unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| {
            events.borrow_mut().push("handler".into());
            Ok(json!({}))
        })
        .unwrap();
    assert_eq!(
        *events.borrow(),
        [
            "a:factory",
            "a:start",
            "b:factory",
            "b:start",
            "handler",
            "b:finish:false",
            "a:finish:false",
            "a:readings",
            "b:readings",
            "a:charge",
            "b:charge"
        ]
    );
    assert_eq!(node.meta["charges"], json!({"a":2,"b":2}));
    assert_eq!(node.meta["a_audit"], json!({"observed":2}));
    assert!(runtime.cleanup_errors().is_empty());
}

#[test]
fn setup_failures_stop_prior_contexts_and_release_sqlite_reservation() {
    for fault in [Fault::Factory, Fault::Start] {
        let events = Events::default();
        let second = Measuring::new("b", &events, fault);
        let control = second.fault.clone();
        let runtime = Runtime::new(SQLiteStore::open(":memory:").unwrap(), ReplayMode::Record)
            .with_meters(vec![
                Rc::new(Measuring::new("a", &events, Fault::None)),
                Rc::new(second),
            ]);
        let mut run = runtime
            .run_with_meters("setup", None, [("a".into(), 5.0)].into(), 0)
            .unwrap();
        let root = run.root_id().to_owned();
        assert!(run
            .model_call(json!({}), CallOptions::default(), |_| panic!(
                "setup failed before provider"
            ))
            .is_err());
        assert_eq!(run.cursor_id(), root);
        assert_eq!(runtime.store().walk(&root).unwrap().len(), 1);
        assert_eq!(events.borrow().last().unwrap(), "a:finish:true");
        assert!(!events.borrow().iter().any(|e| e.starts_with("b:finish")));
        control.set(Fault::None);
        run.model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
            .unwrap();
        assert_eq!(run.report().unwrap().spent["a"], 2.0);
    }
}

#[test]
fn provider_error_remains_primary_when_every_cleanup_fails() {
    let events = Events::default();
    let runtime = Runtime::memory(ReplayMode::Record).with_meters(vec![
        Rc::new(Measuring::new("a", &events, Fault::Finish)),
        Rc::new(Measuring::new("b", &events, Fault::PanicFinish)),
    ]);
    let mut run = runtime.run("primary", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let error = run
        .model_call(json!({}), CallOptions::default(), |_| {
            Err(Error::Handler("provider error".into()))
        })
        .unwrap_err();
    assert_eq!(error, Error::Handler("provider error".into()));
    assert_eq!(run.cursor_id(), root);
    assert!(run.report().unwrap().spent.is_empty());
    assert_eq!(runtime.cleanup_errors().len(), 2);
    assert!(events
        .borrow()
        .ends_with(&["b:finish:true".into(), "a:finish:true".into()]));
}

#[test]
fn post_result_cleanup_and_readings_failures_keep_conservative_charges() {
    for fault in [
        Fault::Finish,
        Fault::PanicFinish,
        Fault::Readings,
        Fault::InvalidReadings,
    ] {
        let events = Events::default();
        let runtime = Runtime::memory(ReplayMode::Record)
            .with_meter(Measuring::new("measured", &events, fault));
        let mut run = runtime.run("post-result", None, 0).unwrap();
        let error = run
            .model_call(json!({}), CallOptions::default(), |_| {
                Ok(json!({"answer":42}))
            })
            .unwrap_err();
        assert!(error.is_post_dispatch_outcome_unknown());
        assert_eq!(run.report().unwrap().spent["measured"], 5.0);
        assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
        assert_eq!(
            events
                .borrow()
                .iter()
                .filter(|e| e.starts_with("measured:finish"))
                .count(),
            1
        );
    }
}

#[test]
fn asynchronous_cancellation_and_handler_panic_stop_once() {
    for panic in [false, true] {
        let events = Events::default();
        let runtime = Runtime::memory(ReplayMode::Record).with_meter(Measuring::new(
            "measured",
            &events,
            Fault::None,
        ));
        let mut run = runtime.run("cancel", None, 0).unwrap();
        if panic {
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                run.model_call(json!({}), CallOptions::default(), |_| {
                    panic!("handler panic")
                })
            }))
            .is_err());
        } else {
            let mut future =
                Box::pin(run.amodel_call(json!({}), CallOptions::default(), |_| pending()));
            assert!(matches!(
                future
                    .as_mut()
                    .poll(&mut Context::from_waker(&noop_waker())),
                Poll::Pending
            ));
            drop(future);
        }
        assert_eq!(
            events
                .borrow()
                .iter()
                .filter(|e| e.as_str() == "measured:finish:true")
                .count(),
            1
        );
        assert_eq!(run.report().unwrap().spent["measured"], 5.0);
        assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
    }
}

#[test]
fn factory_panic_unwinds_already_started_contexts_without_a_pending_charge() {
    let events = Events::default();
    let runtime = Runtime::memory(ReplayMode::Record).with_meters(vec![
        Rc::new(Measuring::new("a", &events, Fault::None)),
        Rc::new(Measuring::new("b", &events, Fault::PanicFactory)),
    ]);
    let mut run = runtime.run("factory-panic", None, 0).unwrap();
    assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run.model_call(json!({}), CallOptions::default(), |_| {
            panic!("not dispatched")
        })
    }))
    .is_err());
    assert_eq!(run.cursor_id(), run.root_id());
    assert!(run.report().unwrap().spent.is_empty());
    assert_eq!(events.borrow().last().unwrap(), "a:finish:true");
}

#[test]
fn replay_and_budget_refusal_never_start_measurements() {
    let events = Events::default();
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(Measuring::new(
        "measured",
        &events,
        Fault::None,
    ));
    let mut run = runtime.run("replay", None, 0).unwrap();
    run.model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    events.borrow_mut().clear();
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay)
        .with_meter(Measuring::new("measured", &events, Fault::Factory));
    replay
        .run("replay", None, 0)
        .unwrap()
        .model_call(json!({}), CallOptions::default(), |_| panic!("replay"))
        .unwrap();
    assert_eq!(*events.borrow(), ["measured:charge"]);
    events.borrow_mut().clear();
    let mut gated = runtime
        .run(
            "gated",
            Some(Budget {
                steps: Some(0),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        gated.model_call(json!({}), CallOptions::default(), |_| panic!("budget")),
        Err(Error::BudgetExceeded { .. })
    ));
    assert!(events.borrow().is_empty());
}

#[test]
fn custom_fallback_applies_with_valid_provider_usage_and_window_forwards_hooks() {
    struct Pricing;
    impl Meter for Pricing {
        fn name(&self) -> &str {
            "usd"
        }
        fn precheck_is_estimate(&self) -> bool {
            true
        }
        fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
            Ok(Some(0.25))
        }
        fn charge(&self, _: NodeKind, _: &Value, _: &Value, _: &Value) -> Result<f64> {
            Ok(0.0)
        }
        fn charge_with_meta(
            &self,
            _: NodeKind,
            _: &Value,
            _: &Value,
            meta: &mut Value,
        ) -> Result<f64> {
            meta["pricing"] = json!({"status":"unavailable"});
            Ok(0.0)
        }
        fn precheck_fallback_reason(
            &self,
            _: NodeKind,
            _: &Value,
            _: &Value,
            _: &Value,
        ) -> Option<String> {
            Some("exact_pricing_unavailable".into())
        }
    }
    let window = WindowMeter::new("usd", 2.0, 60.0, Some(Rc::new(Pricing))).unwrap();
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(window);
    let mut run = runtime.run("fallback", None, 0).unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| {
            Ok(json!({"usage":{"input_tokens":2,"output_tokens":3}}))
        })
        .unwrap();
    assert_eq!(node.meta["charges"]["usd"], json!(0.25));
    assert_eq!(
        node.meta["accounting_fallbacks"]["usd"]["reason"],
        json!("exact_pricing_unavailable")
    );
    assert_eq!(node.meta["pricing"]["status"], json!("unavailable"));
}

struct HookStore {
    inner: MemoryStore,
    fail_exists: Rc<Cell<bool>>,
    staged: Rc<Cell<usize>>,
    fail_stage: bool,
}
impl Store for HookStore {
    fn put(&mut self, node: Node) -> Result<()> {
        assert_ne!(
            node.meta["state"],
            json!("pending"),
            "pending must use stage_pending"
        );
        self.inner.put(node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.inner.get(id)
    }
    fn exists(&self, _: &str) -> bool {
        panic!("runtime must use fallible try_exists")
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        if self.fail_exists.get() {
            Err(Error::Handler("network unavailable".into()))
        } else {
            Ok(self.inner.exists(id))
        }
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.inner.children(id)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.inner.update_meta(id, patch)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.inner.roots()
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.inner.drop_nodes(ids)
    }
}
impl RecordingStore for HookStore {
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.inner.finalize(node)
    }
    fn stage_pending(&mut self, node: Node) -> Result<()> {
        self.staged.set(self.staged.get() + 1);
        if self.fail_stage {
            Err(Error::Handler("stage failed".into()))
        } else {
            self.inner.put(node)
        }
    }
}

#[test]
fn fallible_store_existence_and_pending_hooks_fail_before_provider_dispatch() {
    for fail_stage in [false, true] {
        let events = Events::default();
        let staged = Rc::new(Cell::new(0));
        let fail_exists = Rc::new(Cell::new(false));
        let runtime = Runtime::new(
            HookStore {
                inner: MemoryStore::new(),
                fail_exists: fail_exists.clone(),
                staged: staged.clone(),
                fail_stage,
            },
            ReplayMode::Record,
        )
        .with_meter(Measuring::new("measured", &events, Fault::None));
        let mut run = runtime.run("store-hooks", None, 0).unwrap();
        if fail_stage {
            assert!(run
                .model_call(json!({}), CallOptions::default(), |_| panic!(
                    "stage failed"
                ))
                .is_err());
            assert_eq!(events.borrow().last().unwrap(), "measured:finish:true");
            assert_eq!(run.cursor_id(), run.root_id());
        } else {
            run.model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
                .unwrap();
            assert_eq!(staged.get(), 1);
        }
        events.borrow_mut().clear();
        fail_exists.set(true);
        assert_eq!(
            run.model_call(json!({"second":true}), CallOptions::default(), |_| panic!(
                "remote failure"
            ))
            .unwrap_err(),
            Error::Handler("network unavailable".into())
        );
        assert!(events.borrow().is_empty());
    }
}

#[test]
fn energy_prefers_counter_and_stops_sampling_after_call() {
    let samples = Arc::new(AtomicUsize::new(0));
    let count = samples.clone();
    let counters = Arc::new(AtomicUsize::new(0));
    let meter = EnergyMeter::new(
        Arc::new(move || {
            count.fetch_add(1, Ordering::SeqCst);
            Ok(100.0)
        }),
        0.005,
    )
    .unwrap()
    .with_counter(Arc::new(move || {
        Ok(Some(
            10_000 + 1_500 * counters.fetch_add(1, Ordering::SeqCst) as u64,
        ))
    }));
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(meter);
    let node = runtime
        .run("energy", None, 0)
        .unwrap()
        .model_call(json!({}), CallOptions::default(), |_| {
            std::thread::sleep(Duration::from_millis(15));
            Ok(json!({}))
        })
        .unwrap();
    assert_eq!(node.meta["joules"], json!(1.5));
    assert_eq!(node.meta["charges"]["joules"], json!(1.5));
    let stopped = samples.load(Ordering::SeqCst);
    assert!(stopped >= 2);
    std::thread::sleep(Duration::from_millis(15));
    assert_eq!(samples.load(Ordering::SeqCst), stopped);
}

#[test]
fn energy_counter_unavailability_and_reset_fall_back_to_sampled_power() {
    for mode in 0..3 {
        let calls = AtomicUsize::new(0);
        let counter: EnergyCounter = Arc::new(move || match mode {
            0 => Ok(None),
            1 => Err(Error::Handler("counter unsupported".into())),
            _ => Ok(Some(if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                100
            } else {
                50
            })),
        });
        let mut measurement = EnergyMeter::new(Arc::new(|| Ok(100.0)), 0.05)
            .unwrap()
            .with_counter(counter)
            .measurement();
        measurement.start().unwrap();
        std::thread::sleep(Duration::from_millis(3));
        measurement.finish(None).unwrap();
        assert!(measurement.readings().unwrap()["joules"].as_f64().unwrap() > 0.0);
    }
    assert_eq!(
        integrate_energy(&[(0.0, 100.0), (1.0, 200.0), (3.0, 200.0)]).unwrap(),
        550.0
    );
}

#[test]
fn energy_invalid_intervals_and_start_samples_fail_before_dispatch() {
    for interval in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert!(EnergyMeter::new(Arc::new(|| Ok(1.0)), interval).is_err());
    }
    for watts in [-1.0, f64::NAN, f64::INFINITY] {
        let runtime = Runtime::memory(ReplayMode::Record)
            .with_meter(EnergyMeter::new(Arc::new(move || Ok(watts)), 0.05).unwrap());
        let mut run = runtime.run("bad-energy", None, 0).unwrap();
        assert!(run
            .model_call(json!({}), CallOptions::default(), |_| panic!(
                "invalid sample"
            ))
            .is_err());
        assert_eq!(run.cursor_id(), run.root_id());
        assert!(run.report().unwrap().spent.is_empty());
    }
}

#[test]
fn energy_finish_failure_is_unknown_and_direct_context_drop_stops_worker() {
    let samples = Arc::new(AtomicUsize::new(0));
    let calls = samples.clone();
    let meter = EnergyMeter::new(
        Arc::new(move || {
            if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                Ok(100.0)
            } else {
                Err(Error::Handler("power device disconnected".into()))
            }
        }),
        60.0,
    )
    .unwrap();
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(meter);
    let mut run = runtime.run("energy-disconnect", None, 0).unwrap();
    assert!(run
        .model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap_err()
        .is_post_dispatch_outcome_unknown());
    assert_eq!(samples.load(Ordering::SeqCst), 2);
    assert_eq!(runtime.cleanup_errors().len(), 1);

    let samples = Arc::new(AtomicUsize::new(0));
    let calls = samples.clone();
    let meter = EnergyMeter::new(
        Arc::new(move || {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(100.0)
        }),
        0.001,
    )
    .unwrap();
    {
        let mut measurement = meter.measurement();
        measurement.start().unwrap();
        assert!(measurement.start().is_err());
    }
    let count = samples.load(Ordering::SeqCst);
    std::thread::sleep(Duration::from_millis(5));
    assert_eq!(samples.load(Ordering::SeqCst), count);
}

#[test]
fn window_meter_forwards_measurement_context_and_validates_energy_budget() {
    let counter = AtomicUsize::new(0);
    let energy = EnergyMeter::new(Arc::new(|| Ok(1.0)), 60.0)
        .unwrap()
        .with_counter(Arc::new(move || {
            Ok(Some(counter.fetch_add(1000, Ordering::SeqCst) as u64))
        }));
    let window = WindowMeter::new("joules", 0.5, 60.0, Some(Rc::new(energy))).unwrap();
    let runtime = Runtime::memory(ReplayMode::Record).with_meter(window);
    let mut run = runtime.run("energy-window", None, 0).unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    assert_eq!(node.meta["charges"]["joules"], json!(1));
    assert!(matches!(
        run.model_call(json!({}), CallOptions::default(), |_| panic!(
            "window exhausted"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}

#[test]
fn meter_and_serialization_errors_after_results_preserve_unknown_classification() {
    struct BrokenCharge(bool);
    impl Meter for BrokenCharge {
        fn name(&self) -> &str {
            "measured"
        }
        fn estimate(&self, _: NodeKind, _: &Value) -> Result<Option<f64>> {
            Ok(Some(7.0))
        }
        fn charge(&self, _: NodeKind, _: &Value, _: &Value, _: &Value) -> Result<f64> {
            if self.0 {
                Err(Error::OutcomeUnknown(Box::new(Error::Handler(
                    "meter failure".into(),
                ))))
            } else {
                Ok(f64::NAN)
            }
        }
    }
    for already_wrapped in [false, true] {
        let runtime = Runtime::memory(ReplayMode::Record).with_meter(BrokenCharge(already_wrapped));
        let mut run = runtime.run("meter-post-result", None, 0).unwrap();
        let called = Cell::new(false);
        let error = run
            .model_call(json!({}), CallOptions::default(), |_| {
                called.set(true);
                Ok(json!({}))
            })
            .unwrap_err();
        assert!(called.get());
        assert!(error.is_post_dispatch_outcome_unknown());
        assert!(
            matches!(error,Error::OutcomeUnknown(inner) if !inner.is_post_dispatch_outcome_unknown()),
            "marker must not nest"
        );
        assert_eq!(run.report().unwrap().spent["measured"], 7.0);
    }
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("serialize-post-result", None, 0).unwrap();
    let called = Cell::new(false);
    let error = run
        .model_call(
            json!({}),
            CallOptions {
                estimated_tokens: Some(7),
                ..Default::default()
            },
            |_| {
                called.set(true);
                Ok(json!({"too_large":serde_json::from_str::<Value>("1e999").unwrap()}))
            },
        )
        .unwrap_err();
    assert!(called.get());
    assert!(error.is_post_dispatch_outcome_unknown());
    assert_eq!(run.spent().unwrap().tokens, 7);
    assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
}

struct FailFirstSettlement {
    inner: SQLiteStore,
    attempts: Rc<RefCell<Vec<String>>>,
    fail_stage: bool,
    releases: Rc<Cell<usize>>,
}
impl Store for FailFirstSettlement {
    fn put(&mut self, node: Node) -> Result<()> {
        self.inner.put(node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.inner.get(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.inner.exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.inner.children(id)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.inner.roots()
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.inner.update_meta(id, patch)
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.inner.drop_nodes(ids)
    }
}
impl RecordingStore for FailFirstSettlement {
    fn stage_pending(&mut self, node: Node) -> Result<()> {
        if self.fail_stage {
            Err(Error::Integrity("pending staging failed".into()))
        } else {
            self.inner.stage_pending(node)
        }
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.inner.finalize(node)
    }
    fn supports_reservations(&self) -> bool {
        true
    }
    fn reserve_budget(
        &mut self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        seconds: f64,
    ) -> Result<Option<ReservationCheck>> {
        self.inner.reserve_budget(id, budgets, windows, seconds)
    }
    fn release_budget(&mut self, id: &str) -> Result<()> {
        self.releases.set(self.releases.get() + 1);
        self.inner.release_budget(id)
    }
    fn settle_budget(
        &mut self,
        id: &str,
        charges: &std::collections::BTreeMap<String, rust_decimal::Decimal>,
    ) -> Result<()> {
        let mut attempts = self.attempts.borrow_mut();
        attempts.push(charges["tokens"].to_string());
        if attempts.len() == 1 {
            Err(Error::Integrity("settlement connection lost".into()))
        } else {
            self.inner.settle_budget(id, charges)
        }
    }
}
#[test]
fn settlement_failure_after_callback_is_unknown_and_drop_retries_conservatively() {
    let attempts = Rc::new(RefCell::new(Vec::new()));
    let runtime = Runtime::new(
        FailFirstSettlement {
            inner: SQLiteStore::open(":memory:").unwrap(),
            attempts: attempts.clone(),
            fail_stage: false,
            releases: Rc::new(Cell::new(0)),
        },
        ReplayMode::Record,
    );
    let mut run = runtime
        .run(
            "settlement-fault",
            Some(Budget {
                tokens: Some(10),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let called = Cell::new(false);
    let error = run
        .model_call(
            json!({}),
            CallOptions {
                estimated_tokens: Some(7),
                ..Default::default()
            },
            |_| {
                called.set(true);
                Ok(json!({"usage":{"input_tokens":2,"output_tokens":0}}))
            },
        )
        .unwrap_err();
    assert!(called.get());
    assert!(error.is_post_dispatch_outcome_unknown());
    assert_eq!(*attempts.borrow(), ["2", "7"]);
    assert_eq!(run.spent().unwrap().tokens, 7);
    assert_eq!(run.cursor().unwrap().meta["state"], json!("failed"));
}

#[test]
fn staging_failure_releases_shared_reservation_exactly_once() {
    let releases = Rc::new(Cell::new(0));
    let runtime = Runtime::new(
        FailFirstSettlement {
            inner: SQLiteStore::open(":memory:").unwrap(),
            attempts: Rc::new(RefCell::new(Vec::new())),
            fail_stage: true,
            releases: releases.clone(),
        },
        ReplayMode::Record,
    );
    let mut run = runtime
        .run(
            "stage-once",
            Some(Budget {
                tokens: Some(10),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let error = run
        .model_call(
            json!({}),
            CallOptions {
                estimated_tokens: Some(7),
                ..Default::default()
            },
            |_| panic!("staging failed before dispatch"),
        )
        .unwrap_err();
    assert!(!error.is_post_dispatch_outcome_unknown());
    assert_eq!(releases.get(), 1);
    assert_eq!(run.cursor_id(), run.root_id());
    assert!(run.report().unwrap().spent.is_empty());
}
