use pollardai::{kv::*, *};
use rust_decimal::Decimal;
use std::{
    collections::{BTreeMap, VecDeque},
    str::FromStr,
    sync::{Arc, Mutex},
};
fn d(v: &str) -> Decimal {
    Decimal::from_str(v)
        .or_else(|_| Decimal::from_scientific(v))
        .unwrap()
}
fn map(v: &Value) -> BTreeMap<String, Decimal> {
    v.as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), d(v.as_str().unwrap())))
        .collect()
}
fn budgets(v: &Value) -> Vec<BudgetReservation> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|b| BudgetReservation {
            scope_id: b["scope_id"].as_str().unwrap().into(),
            limits: map(&b["limits"]),
            baseline: map(&b["baseline"]),
            estimates: map(&b["estimates"]),
        })
        .collect()
}
fn windows(v: &Value) -> Vec<WindowReservation> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|w| WindowReservation {
            ledger_key: w["ledger_key"].as_str().unwrap().into(),
            meter: w["meter"].as_str().unwrap().into(),
            limit: d(w["limit"].as_str().unwrap()),
            amount: d(w["amount"].as_str().unwrap()),
            window_seconds: w["window_seconds"].as_f64().unwrap(),
        })
        .collect()
}
fn fixtures() -> Value {
    serde_json::from_str(include_str!("pypi160_kv.json")).unwrap()
}
fn text_map(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().into()))
        .collect()
}
fn text_budgets(value: &Value) -> Vec<TextBudgetReservation> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|v| TextBudgetReservation {
            scope_id: v["scope_id"].as_str().unwrap().into(),
            limits: text_map(&v["limits"]),
            baseline: text_map(&v["baseline"]),
            estimates: text_map(&v["estimates"]),
        })
        .collect()
}
fn text_windows(value: &Value) -> Vec<TextWindowReservation> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|v| TextWindowReservation {
            ledger_key: v["ledger_key"].as_str().unwrap().into(),
            meter: v["meter"].as_str().unwrap().into(),
            limit: v["limit"].as_str().unwrap().into(),
            amount: v["amount"].as_str().unwrap().into(),
            window_seconds: v["window_seconds"].as_f64().unwrap(),
        })
        .collect()
}
#[derive(Clone, Copy)]
enum Fault {
    Before,
    After,
}
#[derive(Clone)]
struct MemoryBackend(Arc<Mutex<State>>);
struct State {
    data: BTreeMap<String, BTreeMap<String, String>>,
    now: f64,
    faults: VecDeque<Fault>,
    reconnects: usize,
}
impl MemoryBackend {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(State {
            data: BTreeMap::from([(
                "schema".into(),
                BTreeMap::from([("version".into(), "1".into())]),
            )]),
            now: 1000.0,
            faults: VecDeque::new(),
            reconnects: 0,
        })))
    }
    fn snapshot(&self) -> Value {
        json!(self.0.lock().unwrap().data)
    }
}
struct Tx {
    data: BTreeMap<String, BTreeMap<String, String>>,
    now: f64,
}
impl KvTransaction for Tx {
    fn get(&mut self, b: &str, k: &str) -> Result<Option<String>> {
        Ok(self.data.get(b).and_then(|b| b.get(k)).cloned())
    }
    fn items(&mut self, b: &str) -> Result<Vec<(String, String)>> {
        Ok(self
            .data
            .get(b)
            .map(|b| b.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default())
    }
    fn put(&mut self, b: &str, k: &str, v: &str) -> Result<()> {
        self.data
            .entry(b.into())
            .or_default()
            .insert(k.into(), v.into());
        Ok(())
    }
    fn delete(&mut self, b: &str, k: &str) -> Result<()> {
        if let Some(b) = self.data.get_mut(b) {
            b.remove(k);
        }
        Ok(())
    }
    fn now(&self) -> f64 {
        self.now
    }
}
fn disconnected() -> Error {
    Error::Backend {
        detail: "injected connection loss".into(),
        connection_lost: true,
    }
}
impl KvBackend for MemoryBackend {
    fn transact(
        &self,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()> {
        let mut state = self.0.lock().unwrap();
        let fault = state.faults.pop_front();
        if matches!(fault, Some(Fault::Before)) {
            return Err(disconnected());
        }
        let mut tx = Tx {
            data: state.data.clone(),
            now: state.now,
        };
        callback(&mut tx)?;
        assert!(
            writable || tx.data == state.data,
            "read transaction mutated"
        );
        if writable {
            state.data = tx.data;
        }
        if matches!(fault, Some(Fault::After)) {
            return Err(disconnected());
        }
        Ok(())
    }
    fn reconnect(&self) -> Result<()> {
        self.0.lock().unwrap().reconnects += 1;
        Ok(())
    }
}
#[test]
fn frozen_wire_codecs_preserve_decimal_scale_unicode_and_raw_result() {
    let f = fixtures();
    for row in f["requests"].as_array().unwrap() {
        let (text, digest) = reservation_request(
            &budgets(&row["budgets"]),
            &windows(&row["windows"]),
            row["lease"].as_f64().unwrap(),
        )
        .unwrap();
        assert_eq!(text, row["request"]);
        assert_eq!(digest, row["digest"]);
        let (text, digest) = reservation_charges(&map(&row["charges"])).unwrap();
        assert_eq!(text, row["charges_text"]);
        assert_eq!(digest, row["charges_digest"]);
    }
    let backend = MemoryBackend::new();
    let mut store = TransactionalKvStore::from_backend(backend, false).unwrap();
    for row in f["nodes"].as_array().unwrap() {
        let raw = row["node_text"].as_str().unwrap();
        let node = node_from_text(raw).unwrap();
        node.validate().unwrap();
        assert_eq!(node_text(&node).unwrap(), raw);
        store.put(node.clone()).unwrap();
        assert_eq!(store.get(&node.id).unwrap(), node);
    }
}

#[test]
fn decimal_text_fingerprints_and_retry_tombstones_match_frozen_python() {
    let old = fixtures();
    let fixture: Value = serde_json::from_str(include_str!("pypi160_decimal_wire.json")).unwrap();
    for row in old["requests"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["requests"].as_array().unwrap())
    {
        let budgets = text_budgets(&row["budgets"]);
        let windows = text_windows(&row["windows"]);
        let charges = text_map(&row["charges"]);
        let lease = row["lease"].as_f64().unwrap();
        assert_eq!(
            reservation_request_text(&budgets, &windows, lease).unwrap(),
            (
                row["request"].as_str().unwrap().into(),
                row["digest"].as_str().unwrap().into()
            )
        );
        assert_eq!(
            reservation_charges_text(&charges).unwrap(),
            (
                row["charges_text"].as_str().unwrap().into(),
                row["charges_digest"].as_str().unwrap().into()
            )
        );
        if row.get("reserved").is_none() {
            continue;
        }
        let backend = MemoryBackend::new();
        let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
        backend.0.lock().unwrap().faults.push_back(Fault::After);
        assert!(
            store
                .reserve_decimal_text("decimal", &budgets, &windows, lease)
                .unwrap()
                .ok
        );
        assert!(
            store
                .reserve_decimal_text("decimal", &budgets, &windows, lease)
                .unwrap()
                .ok
        );
        assert_eq!(
            backend.snapshot(),
            row["reserved"],
            "reserve {}",
            row["charges"]
        );
        backend.0.lock().unwrap().faults.push_back(Fault::After);
        store.settle_decimal_text("decimal", &charges).unwrap();
        store.settle_decimal_text("decimal", &charges).unwrap();
        assert_eq!(
            backend.snapshot(),
            row["settled"],
            "settle {}",
            row["charges"]
        );
        let before = backend.snapshot();
        let changed = BTreeMap::from([("usd".into(), "999.00".into())]);
        assert!(store.settle_decimal_text("decimal", &changed).is_err());
        assert_eq!(backend.snapshot(), before);
    }
}

#[test]
fn exact_ledger_rounding_failure_rolls_back_reserve_and_settlement() {
    let backend = MemoryBackend::new();
    let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
    let key = compound_key(&["large", "usd"]).unwrap();
    backend.0.lock().unwrap().data.insert(
        "budget".into(),
        BTreeMap::from([(key.clone(), Decimal::MAX.to_string())]),
    );
    let request = BudgetReservation {
        scope_id: "large".into(),
        limits: BTreeMap::from([("usd".into(), Decimal::MAX)]),
        ..Default::default()
    };
    store.reserve("large", &[request], &[], 60.0).unwrap();
    let before = backend.snapshot();
    assert!(store
        .settle(
            "large",
            &BTreeMap::from([("usd".into(), parse_decimal_exact("1e-28").unwrap())])
        )
        .is_err());
    assert_eq!(backend.snapshot(), before);
    backend
        .0
        .lock()
        .unwrap()
        .data
        .get_mut("budget")
        .unwrap()
        .insert(key, "1e-28".into());
    let before = backend.snapshot();
    let request = BudgetReservation {
        scope_id: "large".into(),
        limits: BTreeMap::from([("usd".into(), Decimal::MAX)]),
        ..Default::default()
    };
    assert!(store
        .reserve("unrepresentable", &[request], &[], 60.0)
        .is_err());
    assert_eq!(backend.snapshot(), before);
    backend
        .0
        .lock()
        .unwrap()
        .data
        .get_mut("budget")
        .unwrap()
        .insert(compound_key(&["large", "usd"]).unwrap(), "-1".into());
    let before = backend.snapshot();
    assert!(store.settle("large", &BTreeMap::new()).is_err());
    assert_eq!(backend.snapshot(), before);
}
#[test]
fn frozen_transactional_ledger_trace_matches_every_committed_bucket() {
    let f = fixtures();
    let backend = MemoryBackend::new();
    let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
    for (i, row) in f["trace"].as_array().unwrap().iter().enumerate() {
        let a = &row["action"];
        let id = a["id"].as_str().unwrap_or("");
        let result = match a["op"].as_str().unwrap() {
            "reserve" => store
                .reserve(
                    id,
                    &budgets(&a["budgets"]),
                    &windows(&a["windows"]),
                    a["lease"].as_f64().unwrap(),
                )
                .map(|check| {
                    let expected = &row["check"];
                    assert_eq!(check.ok, expected["ok"].as_bool().unwrap(), "step{i}");
                    assert_eq!(check.reason, expected["reason"].as_str().unwrap());
                    assert_eq!(json!(check.meter), expected["meter"]);
                    assert_eq!(check.requested, d(expected["requested"].as_str().unwrap()));
                    assert_eq!(check.remaining, d(expected["remaining"].as_str().unwrap()));
                }),
            "settle" => store.settle(id, &map(&a["charges"])),
            "release" => store.release(id),
            "renew" => store
                .renew(id, a["lease"].as_f64().unwrap())
                .map(|renewed| assert_eq!(json!(renewed), row["renewed"])),
            "advance" => {
                backend.0.lock().unwrap().now += a["seconds"].as_f64().unwrap();
                Ok(())
            }
            _ => unreachable!(),
        };
        assert_eq!(
            result.is_err(),
            row.get("error").is_some(),
            "step{i}: {result:?}"
        );
        assert_eq!(backend.snapshot(), row["state"], "step{i}");
    }
}
#[test]
fn lost_commit_ack_retries_once_and_tombstones_prevent_double_charges() {
    let f = fixtures();
    let row = &f["requests"][0];
    let backend = MemoryBackend::new();
    let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
    backend.0.lock().unwrap().faults.push_back(Fault::After);
    assert!(
        store
            .reserve(
                "retry",
                &budgets(&row["budgets"]),
                &windows(&row["windows"]),
                30.0
            )
            .unwrap()
            .ok
    );
    assert_eq!(backend.0.lock().unwrap().reconnects, 1);
    backend.0.lock().unwrap().faults.push_back(Fault::After);
    store
        .settle("retry", &BTreeMap::from([("usd".into(), d("0.0000001"))]))
        .unwrap();
    assert_eq!(backend.0.lock().unwrap().reconnects, 2);
    let snapshot = backend.snapshot();
    store
        .settle("retry", &BTreeMap::from([("usd".into(), d("0.0000001"))]))
        .unwrap();
    assert_eq!(backend.snapshot(), snapshot);
    backend
        .0
        .lock()
        .unwrap()
        .faults
        .extend([Fault::Before, Fault::Before]);
    assert!(matches!(
        store.reserve(
            "uncertain",
            &budgets(&row["budgets"]),
            &windows(&row["windows"]),
            30.0
        ),
        Err(Error::ReservationUncertain { .. })
    ));
    backend
        .0
        .lock()
        .unwrap()
        .faults
        .extend([Fault::Before, Fault::Before]);
    assert!(matches!(
        store.settle("retry", &BTreeMap::new()),
        Err(Error::SettlementUncertain { .. })
    ));
    backend
        .0
        .lock()
        .unwrap()
        .faults
        .extend([Fault::Before, Fault::Before]);
    assert!(matches!(
        store.try_exists("missing"),
        Err(Error::Backend {
            connection_lost: true,
            ..
        })
    ));
}
#[test]
fn readonly_and_corrupt_clock_never_mutate_and_batches_roll_back() {
    let backend = MemoryBackend::new();
    let mut store = TransactionalKvStore::from_backend(backend.clone(), true).unwrap();
    let root = Node::make(NodeKind::Root, None, 0, json!({"run":"x"}), None, json!({})).unwrap();
    let before = backend.snapshot();
    assert!(store.put(root.clone()).is_err());
    assert_eq!(before, backend.snapshot());
    let mut store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
    let child = Node::make(
        NodeKind::Note,
        Some(&"a".repeat(64)),
        0,
        json!({}),
        None,
        json!({}),
    )
    .unwrap();
    assert!(store.apply_batch(vec![root, child], Vec::new()).is_err());
    assert_eq!(before, backend.snapshot());
    backend.0.lock().unwrap().now = f64::NAN;
    assert!(store.roots().is_err());
}
#[test]
fn overflowing_expiry_rejects_without_corrupting_ledger_or_tombstone() {
    let backend = MemoryBackend::new();
    let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
    let budgets = vec![BudgetReservation {
        scope_id: "overflow".into(),
        limits: BTreeMap::from([("steps".into(), Decimal::ONE)]),
        estimates: BTreeMap::from([("steps".into(), Decimal::ONE)]),
        ..Default::default()
    }];
    backend.0.lock().unwrap().now = f64::MAX / 2.0;
    let before = backend.snapshot();
    assert!(store.reserve("overflow", &budgets, &[], f64::MAX).is_err());
    assert_eq!(backend.snapshot(), before);
    assert!(
        store
            .reserve("valid", &budgets, &[], f64::MAX / 4.0)
            .unwrap()
            .ok
    );
    let before = backend.snapshot();
    assert!(store.renew("valid", f64::MAX).is_err());
    assert_eq!(backend.snapshot(), before);
}
#[test]
fn imported_budget_decimals_never_round_silently() {
    for amount in [
        "-1",
        "1e-29",
        "0.00000000000000000000000000001",
        "1.23456789012345678901234567891",
    ] {
        let backend = MemoryBackend::new();
        let store = TransactionalKvStore::from_backend(backend.clone(), false).unwrap();
        backend.0.lock().unwrap().data.insert(
            "budget".into(),
            BTreeMap::from([(compound_key(&["scope", "steps"]).unwrap(), amount.into())]),
        );
        let before = backend.snapshot();
        let budget = BudgetReservation {
            scope_id: "scope".into(),
            limits: BTreeMap::from([("steps".into(), Decimal::ONE)]),
            ..Default::default()
        };
        assert!(
            store.reserve("bad-ledger", &[budget], &[], 30.0).is_err(),
            "{amount}"
        );
        assert_eq!(backend.snapshot(), before);
    }
}
