#![cfg(feature = "postgres")]
use pollardai::*;
use postgres::{Client, NoTls};
use rust_decimal::Decimal;
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Barrier,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Database {
    admin: Client,
    schema: String,
    dsn: String,
}
impl Database {
    fn open() -> Option<Self> {
        let base = std::env::var("POLLARD_TEST_POSTGRES_DSN")
            .expect("POLLARD_TEST_POSTGRES_DSN is required");
        let schema = format!(
            "pollard_rust_{}_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        let mut admin = Client::connect(&base, NoTls).unwrap();
        admin
            .batch_execute(&format!("CREATE SCHEMA {schema}"))
            .unwrap();
        let dsn = format!("{base} options='-c search_path={schema}'");
        Some(Self { admin, schema, dsn })
    }
    fn store(&self, id: &str) -> PostgresStore {
        PostgresStore::connect_with_options(
            self.dsn.clone(),
            PostgresOptions {
                store_id: id.into(),
                intern_threshold: Some(8),
                ..Default::default()
            },
        )
        .unwrap()
    }
    fn client(&self) -> Client {
        Client::connect(&self.dsn, NoTls).unwrap()
    }
}
impl Drop for Database {
    fn drop(&mut self) {
        let _ = self
            .admin
            .batch_execute(&format!("DROP SCHEMA {} CASCADE", self.schema));
    }
}
fn root(label: &str) -> Node {
    Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":label}),
        None,
        json!({}),
    )
    .unwrap()
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn decimal_text_retries_match_python_fingerprints_and_rounded_settlement_rolls_back() {
    let database = Database::open().unwrap();
    let store = database.store("decimal-text");
    let fixture: Value = serde_json::from_str(include_str!("pypi160_decimal_wire.json")).unwrap();
    for (index, row) in fixture["requests"].as_array().unwrap().iter().enumerate() {
        if row.get("reserved").is_none() {
            continue;
        }
        let map = |v: &Value| {
            v.as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().into()))
                .collect::<BTreeMap<String, String>>()
        };
        let b = &row["budgets"][0];
        let w = &row["windows"][0];
        let b = TextBudgetReservation {
            scope_id: b["scope_id"].as_str().unwrap().into(),
            limits: map(&b["limits"]),
            baseline: map(&b["baseline"]),
            estimates: map(&b["estimates"]),
        };
        let w = TextWindowReservation {
            ledger_key: w["ledger_key"].as_str().unwrap().into(),
            meter: w["meter"].as_str().unwrap().into(),
            limit: w["limit"].as_str().unwrap().into(),
            amount: w["amount"].as_str().unwrap().into(),
            window_seconds: w["window_seconds"].as_f64().unwrap(),
        };
        let id = format!("decimal-{index}");
        assert!(
            store
                .reserve_decimal_text(
                    &id,
                    std::slice::from_ref(&b),
                    std::slice::from_ref(&w),
                    60.0
                )
                .unwrap()
                .ok
        );
        assert!(
            store
                .reserve_decimal_text(&id, &[b], &[w], 60.0)
                .unwrap()
                .ok
        );
        let record=database.client().query_one("SELECT request,request_digest FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2",&[&"decimal-text",&id]).unwrap();
        assert_eq!(record.get::<_, &str>(0), row["request"].as_str().unwrap());
        assert_eq!(record.get::<_, &str>(1), row["digest"].as_str().unwrap());
        store
            .settle_decimal_text(&id, &map(&row["charges"]))
            .unwrap();
        store
            .settle_decimal_text(&id, &map(&row["charges"]))
            .unwrap();
        let record=database.client().query_one("SELECT charges,charges_digest FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2",&[&"decimal-text",&id]).unwrap();
        assert_eq!(
            record.get::<_, &str>(0),
            row["charges_text"].as_str().unwrap()
        );
        assert_eq!(
            record.get::<_, &str>(1),
            row["charges_digest"].as_str().unwrap()
        );
    }
    let map = |n: Decimal| BTreeMap::from([("usd".into(), n)]);
    let b = BudgetReservation {
        scope_id: "large".into(),
        limits: map(Decimal::MAX),
        baseline: map(Decimal::MAX),
        ..Default::default()
    };
    store.reserve("large", &[b], &[], 60.0).unwrap();
    assert!(store
        .settle("large", &map(parse_decimal_exact("1e-28").unwrap()))
        .is_err());
    let row = database
        .client()
        .query_one(
            "SELECT settled::text FROM pollard_budget_state WHERE store_id=$1 AND scope_id='large'",
            &[&"decimal-text"],
        )
        .unwrap();
    assert_eq!(row.get::<_, &str>(0), Decimal::MAX.to_string());
    let row=database.client().query_one("SELECT state FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id='large'",&[&"decimal-text"]).unwrap();
    assert_eq!(row.get::<_, &str>(0), "active");
}
fn budget(scope: &str, limit: i64) -> BudgetReservation {
    BudgetReservation {
        scope_id: scope.into(),
        limits: BTreeMap::from([("steps".into(), Decimal::from(limit))]),
        estimates: BTreeMap::from([("steps".into(), Decimal::ONE)]),
        ..Default::default()
    }
}
fn charges(value: i64) -> BTreeMap<String, Decimal> {
    BTreeMap::from([("steps".into(), Decimal::from(value))])
}
fn window() -> WindowReservation {
    WindowReservation {
        ledger_key: "window".into(),
        meter: "requests".into(),
        limit: Decimal::from(3),
        amount: Decimal::ONE,
        window_seconds: 60.0,
    }
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn native_storage_preserves_exact_results_interning_and_isolation() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut store = db.store("a");
    let root = root("unicode-é");
    store.put(root.clone()).unwrap();
    let node = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"prompt":"é東京abcdefghij","literal":{"__pollard_ref":"0".repeat(64)}}),
        Some(json!({"float":1e-5,"usage":{"input_tokens":1,"output_tokens":2}})),
        json!({"finite":1e-5}),
    )
    .unwrap();
    store.put(node.clone()).unwrap();
    assert_eq!(store.get(&node.id).unwrap(), node);
    assert_eq!(
        store.children(&root.id).unwrap(),
        std::slice::from_ref(&node.id)
    );
    assert!(db.store("b").roots().unwrap().is_empty());
    let mut client = db.client();
    assert_eq!(
        client
            .query_one("SELECT version FROM pollard_schema", &[])
            .unwrap()
            .get::<_, i32>(0),
        2
    );
    assert!(
        client
            .query_one("SELECT count(*) FROM pollard_blobs", &[])
            .unwrap()
            .get::<_, i64>(0)
            > 0
    );
    assert_eq!(
        client
            .query_one("SELECT count(*) FROM pollard_blob_literals", &[])
            .unwrap()
            .get::<_, i64>(0),
        1
    );
    assert!(verify_subtree(&store, &root.id).ok);
    let manifest = export_manifest(&store, &root.id).unwrap();
    let mut copy = db.store("copy");
    import_manifest(manifest, &mut copy).unwrap();
    assert_eq!(
        seal(&copy, &root.id).unwrap(),
        seal(&store, &root.id).unwrap()
    );
    store.reconnect().unwrap();
    assert_eq!(store.get(&node.id).unwrap(), node);
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn runtime_records_replays_and_respects_shared_budget() {
    let Some(db) = Database::open() else {
        return;
    };
    let rt = Runtime::new(db.store("runtime"), ReplayMode::Record);
    let mut run = rt
        .run(
            "live",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let expected = run
        .model_call(json!({"x":1}), CallOptions::default(), |_| {
            Ok(json!({"text":"yes","usage":{"input_tokens":2,"output_tokens":3}}))
        })
        .unwrap();
    assert_eq!(run.spent().unwrap().steps, 1);
    assert!(matches!(
        run.model_call(json!({"x":2}), CallOptions::default(), |_| panic!(
            "budget dispatch"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    let replay = Runtime::new(db.store("runtime"), ReplayMode::Replay);
    let mut run = replay
        .run(
            "live",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert_eq!(
        run.model_call(json!({"x":1}), CallOptions::default(), |_| panic!(
            "replay dispatch"
        ))
        .unwrap(),
        expected
    );
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn parallel_writers_preserve_first_result_and_all_metadata() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut store = db.store("writers");
    let root = root("r");
    store.put(root.clone()).unwrap();
    let first = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({}),
        Some(json!({"first":true})),
        json!({}),
    )
    .unwrap();
    store.put(first.clone()).unwrap();
    let mut tasks = Vec::new();
    let barrier = Arc::new(Barrier::new(8));
    for index in 0..8 {
        let dsn = db.dsn.clone();
        let id = first.id.clone();
        let barrier = barrier.clone();
        let mut conflicting = first.clone();
        conflicting.result = Some(json!({"other":true}));
        let (t, d) = result_text_and_digest(conflicting.result.as_ref().unwrap()).unwrap();
        conflicting.result_text = Some(t);
        conflicting.result_digest = Some(d);
        tasks.push(std::thread::spawn(move || {
            let mut s = PostgresStore::connect_with_options(
                dsn,
                PostgresOptions {
                    store_id: "writers".into(),
                    ..Default::default()
                },
            )
            .unwrap();
            barrier.wait();
            s.put(conflicting).unwrap();
            s.update_meta(&id, json!({format!("m{index}"):index}))
                .unwrap();
        }));
    }
    for task in tasks {
        task.join().unwrap();
    }
    let node = store.get(&first.id).unwrap();
    assert_eq!(node.result, first.result);
    assert_eq!(node.meta["result_conflicts"].as_array().unwrap().len(), 1);
    for i in 0..8 {
        assert_eq!(node.meta[format!("m{i}")], i);
    }
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn import_merge_batch_rollback_and_gc_are_transactional() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut store = db.store("merge");
    let root = root("r");
    store.put(root.clone()).unwrap();
    let child = Node::make(
        NodeKind::Note,
        Some(&root.id),
        0,
        json!({"text":"large blob for gc"}),
        None,
        json!({"pruned":true}),
    )
    .unwrap();
    store.put(child.clone()).unwrap();
    let before = seal(&store, &root.id).unwrap();
    let missing = Node::make(
        NodeKind::Note,
        Some(&"0".repeat(64)),
        0,
        json!({}),
        None,
        json!({}),
    )
    .unwrap();
    let extra = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"extra"}),
        None,
        json!({}),
    )
    .unwrap();
    assert!(store
        .apply_batch(vec![extra.clone(), missing], vec![])
        .is_err());
    assert!(!store.try_exists(&extra.id).unwrap());
    assert_eq!(before, seal(&store, &root.id).unwrap());
    let mut source = MemoryStore::new();
    source.put(root.clone()).unwrap();
    source.put(child.clone()).unwrap();
    source.update_meta(&root.id, json!({"source":1})).unwrap();
    assert_eq!(merge(&mut store, &source, false).unwrap().copied, 0);
    assert_eq!(store.get(&root.id).unwrap().meta["source"], 1);
    assert_eq!(merge(&mut store, &source, false).unwrap().meta_conflicts, 0);
    assert_eq!(gc(&mut store, "drop-pruned").unwrap().removed_nodes, 1);
    assert!(!store.try_exists(&child.id).unwrap());
    assert!(gc(&mut store, "compact").unwrap().removed_blobs > 0);
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn reservation_tombstones_exact_amounts_and_expiry() {
    let Some(db) = Database::open() else {
        return;
    };
    let store = db.store("budget");
    let b = budget("root", 2);
    assert!(
        store
            .reserve("a", std::slice::from_ref(&b), &[], 10.0)
            .unwrap()
            .ok
    );
    assert!(
        store
            .reserve("a", std::slice::from_ref(&b), &[], 10.0)
            .unwrap()
            .ok
    );
    assert!(store.reserve("a", &[budget("root", 3)], &[], 10.0).is_err());
    store.settle("a", &charges(1)).unwrap();
    store.settle("a", &charges(1)).unwrap();
    assert!(store.settle("a", &charges(2)).is_err());
    assert!(store
        .reserve("a", std::slice::from_ref(&b), &[], 10.0)
        .is_err());
    assert!(store.release("a").is_err());
    assert!(
        store
            .reserve("late", std::slice::from_ref(&b), &[], 0.1)
            .unwrap()
            .ok
    );
    db.client()
        .execute(
            "UPDATE pollard_reservation_state SET expires_at=0 WHERE reservation_id='late'",
            &[],
        )
        .unwrap();
    db.client()
        .execute(
            "UPDATE pollard_reservations SET expires_at=0 WHERE reservation_id='late'",
            &[],
        )
        .unwrap();
    assert!(!store.renew("late", 1.0).unwrap());
    assert!(store
        .reserve("late", std::slice::from_ref(&b), &[], 0.1)
        .is_err());
    store.settle("late", &charges(1)).unwrap();
    assert!(!store.reserve("over", &[b], &[], 1.0).unwrap().ok);
    let mut exact = budget("money", 1);
    exact.limits = BTreeMap::from([("usd".into(), Decimal::new(3, 1))]);
    exact.estimates = BTreeMap::from([("usd".into(), Decimal::new(1, 1))]);
    for (i, amount) in [1, 2].into_iter().enumerate() {
        assert!(
            store
                .reserve(&format!("usd{i}"), std::slice::from_ref(&exact), &[], 1.0)
                .unwrap()
                .ok
        );
        store
            .settle(
                &format!("usd{i}"),
                &BTreeMap::from([("usd".into(), Decimal::new(amount, 1))]),
            )
            .unwrap();
    }
    assert!(!store.reserve("usd-over", &[exact], &[], 1.0).unwrap().ok);
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn windows_atomically_limit_eight_connections_and_renew() {
    let Some(db) = Database::open() else {
        return;
    };
    let store = db.store("windows");
    let barrier = Arc::new(Barrier::new(8));
    let mut tasks = Vec::new();
    for index in 0..8 {
        let dsn = db.dsn.clone();
        let barrier = barrier.clone();
        tasks.push(std::thread::spawn(move || {
            let s = PostgresStore::connect_with_options(
                dsn,
                PostgresOptions {
                    store_id: "windows".into(),
                    ..Default::default()
                },
            )
            .unwrap();
            barrier.wait();
            (
                index,
                s.reserve(&format!("w{index}"), &[], &[window()], 10.0)
                    .unwrap()
                    .ok,
            )
        }));
    }
    let accepted: Vec<_> = tasks
        .into_iter()
        .map(|t| t.join().unwrap())
        .filter(|(_, ok)| *ok)
        .map(|(i, _)| format!("w{i}"))
        .collect();
    assert_eq!(accepted.len(), 3);
    assert!(store.renew(&accepted[0], 60.0).unwrap());
    store
        .settle(
            &accepted[0],
            &BTreeMap::from([("requests".into(), Decimal::ONE)]),
        )
        .unwrap();
    assert!(!store.reserve("over", &[], &[window()], 10.0).unwrap().ok);
    store.release(&accepted[1]).unwrap();
    store.release(&accepted[1]).unwrap();
    assert!(
        store
            .reserve("replacement", &[], &[window()], 10.0)
            .unwrap()
            .ok
    );
    assert!(store.reserve(&accepted[1], &[], &[window()], 10.0).is_err());
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn read_only_and_reconnect_never_repair_missing_schema() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut store = db.store("ro");
    let root = root("r");
    store.put(root.clone()).unwrap();
    let mut read = PostgresStore::connect_with_options(
        db.dsn.clone(),
        PostgresOptions {
            store_id: "ro".into(),
            read_only: true,
            ..Default::default()
        },
    )
    .unwrap();
    assert_eq!(read.get(&root.id).unwrap(), root);
    assert!(read.put(root.clone()).is_err());
    assert!(read.reserve("r", &[budget("r", 1)], &[], 1.0).is_err());
    db.client()
        .batch_execute("DROP TABLE pollard_reservation_state")
        .unwrap();
    assert!(read.reconnect().is_err());
    assert!(store.reconnect().is_err());
    assert!(!db
        .client()
        .query_one(
            "SELECT to_regclass('pollard_reservation_state') IS NOT NULL",
            &[]
        )
        .unwrap()
        .get::<_, bool>(0));
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn explicit_migration_requires_drained_valid_legacy_schema() {
    let Some(db) = Database::open() else {
        return;
    };
    let store = db.store("migration");
    drop(store);
    let mut client = db.client();
    client.batch_execute("DROP TABLE pollard_reservation_state;UPDATE pollard_schema SET version=1;INSERT INTO pollard_reservations VALUES('migration','held','budget','r','steps',1,0,NULL)").unwrap();
    assert!(PostgresStore::connect(db.dsn.clone()).is_err());
    assert!(PostgresStore::migrate(db.dsn.clone()).is_err());
    client
        .execute("DELETE FROM pollard_reservations", &[])
        .unwrap();
    assert_eq!(PostgresStore::migrate(db.dsn.clone()).unwrap(), (1, 2));
    assert_eq!(PostgresStore::migrate(db.dsn.clone()).unwrap(), (2, 2));
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn reconnect_after_backend_termination_and_persistent_uncertainty() {
    let Some(db) = Database::open() else {
        return;
    };
    let failed = Arc::new(AtomicBool::new(false));
    let pid = Arc::new(AtomicU64::new(0));
    let failure = failed.clone();
    let backend_pid = pid.clone();
    let dsn = db.dsn.clone();
    let mut store = PostgresStore::connect_with_factory(
        move || {
            if failure.load(Ordering::SeqCst) {
                return Err(Error::Backend {
                    detail: "injected offline connector".into(),
                    connection_lost: true,
                });
            }
            let mut c = Client::connect(&dsn, NoTls).unwrap();
            backend_pid.store(
                c.query_one("SELECT pg_backend_pid()", &[])
                    .unwrap()
                    .get::<_, i32>(0) as u64,
                Ordering::SeqCst,
            );
            Ok(c)
        },
        PostgresOptions::default(),
    )
    .unwrap();
    let root = root("r");
    store.put(root.clone()).unwrap();
    db.client()
        .query_one(
            "SELECT pg_terminate_backend($1)",
            &[&(pid.load(Ordering::SeqCst) as i32)],
        )
        .unwrap();
    assert_eq!(store.get(&root.id).unwrap(), root);
    assert!(
        store
            .reserve("held", &[budget("r", 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    failed.store(true, Ordering::SeqCst);
    db.client()
        .query_one(
            "SELECT pg_terminate_backend($1)",
            &[&(pid.load(Ordering::SeqCst) as i32)],
        )
        .unwrap();
    assert!(matches!(
        store.settle("held", &charges(1)),
        Err(Error::SettlementUncertain { .. })
    ));
    assert!(matches!(
        store.reserve("next", &[budget("r", 1)], &[], 1.0),
        Err(Error::ReservationUncertain { .. })
    ));
    assert!(store.try_exists(&root.id).is_err());
}

#[test]
#[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
fn reservation_clock_is_sampled_after_row_lock_wait() {
    let Some(db) = Database::open() else {
        return;
    };
    let store = db.store("clock");
    assert!(
        store
            .reserve("init", &[budget("root", 2)], &[], 1.0)
            .unwrap()
            .ok
    );
    store.release("init").unwrap();
    let mut lock = db.client();
    let mut tx = lock.transaction().unwrap();
    tx.query(
        "SELECT settled FROM pollard_budget_state WHERE store_id='clock' FOR UPDATE",
        &[],
    )
    .unwrap();
    let dsn = db.dsn.clone();
    let started = Arc::new(Barrier::new(2));
    let signal = started.clone();
    let task = std::thread::spawn(move || {
        let s = PostgresStore::connect_with_options(
            dsn,
            PostgresOptions {
                store_id: "clock".into(),
                ..Default::default()
            },
        )
        .unwrap();
        signal.wait();
        s.reserve("waited", &[budget("root", 2)], &[], 0.2).unwrap()
    });
    started.wait();
    std::thread::sleep(Duration::from_millis(300));
    tx.commit().unwrap();
    assert!(task.join().unwrap().ok);
    let remaining:f64=db.client().query_one("SELECT expires_at-EXTRACT(EPOCH FROM clock_timestamp())::double precision FROM pollard_reservation_state WHERE store_id='clock' AND reservation_id='waited'",&[]).unwrap().get(0);
    assert!(
        remaining > 0.1,
        "lease was consumed waiting for lock: {remaining}"
    );
}
