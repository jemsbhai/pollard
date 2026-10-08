#![cfg(feature = "neo4j")]
use neo4j_driver::{
    driver::{auth::AuthToken, ConnectionConfig, Driver, DriverConfig},
    value_map,
};
use pollardai::*;
use rust_decimal::Decimal;
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Barrier,
    },
    time::{SystemTime, UNIX_EPOCH},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Database {
    uri: String,
    user: String,
    password: String,
    id: String,
    driver: Driver,
}
impl Database {
    fn open() -> Self {
        let uri = std::env::var("POLLARD_TEST_NEO4J_URI").expect("POLLARD_TEST_NEO4J_URI required");
        let user = std::env::var("POLLARD_TEST_NEO4J_USER").unwrap_or("neo4j".into());
        let password = std::env::var("POLLARD_TEST_NEO4J_PASSWORD")
            .expect("POLLARD_TEST_NEO4J_PASSWORD required");
        let id = format!(
            "pollard_rust_{}_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        let driver = Driver::new(
            ConnectionConfig::try_from(uri.as_str()).unwrap(),
            DriverConfig::new().with_auth(Arc::new(AuthToken::new_basic_auth(&user, &password))),
        );
        Self {
            uri,
            user,
            password,
            id,
            driver,
        }
    }
    fn options(&self, suffix: &str) -> Neo4jOptions {
        Neo4jOptions {
            store_id: format!("{}_{}", self.id, suffix),
            ..Default::default()
        }
    }
    fn store(&self, suffix: &str) -> Neo4jStore {
        Neo4jStore::connect_with_options(
            &self.uri,
            &self.user,
            &self.password,
            self.options(suffix),
        )
        .unwrap()
    }
    fn query(&self, text: &str) {
        self.driver
            .execute_query(text)
            .with_parameters(value_map!({"store":self.options("a").store_id}))
            .with_database(Arc::new("neo4j".into()))
            .run()
            .unwrap();
    }
}
impl Drop for Database {
    fn drop(&mut self) {
        let _=self.driver.execute_query("MATCH (n) WHERE (n:_PollardKV OR n:_PollardCoordinator) AND n.store_id STARTS WITH $prefix DETACH DELETE n").with_parameters(value_map!({"prefix":format!("{}_",self.id)})).with_database(Arc::new("neo4j".into())).run();
    }
}
fn root() -> Node {
    Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"東京"}),
        None,
        json!({}),
    )
    .unwrap()
}
fn budget() -> BudgetReservation {
    BudgetReservation {
        scope_id: "root".into(),
        limits: BTreeMap::from([("usd".into(), Decimal::new(3, 1))]),
        estimates: BTreeMap::from([("usd".into(), Decimal::new(1, 1))]),
        ..Default::default()
    }
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password"]
fn native_routed_records_manifests_seals_bookmarks_and_rollback() {
    let db = Database::open();
    assert!(
        db.uri.starts_with("neo4j://"),
        "live routing test must use neo4j://"
    );
    let mut a = db.store("a");
    let root = root();
    a.put(root.clone()).unwrap();
    let node = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"p":"é東京","big":12345678901234567890u64}),
        Some(json!({"float":1e-5,"negative":-0.0})),
        json!({}),
    )
    .unwrap();
    a.put(node.clone()).unwrap();
    for i in 0..6 {
        a.update_meta(&node.id, json!({"round":i})).unwrap();
        assert_eq!(a.get(&node.id).unwrap().meta["round"], i);
    }
    assert!(verify_subtree(&a, &root.id).ok);
    let mut copy = db.store("copy");
    import_manifest(export_manifest(&a, &root.id).unwrap(), &mut copy).unwrap();
    assert_eq!(seal(&a, &root.id).unwrap(), seal(&copy, &root.id).unwrap());
    a.reconnect().unwrap();
    assert_eq!(a.get(&node.id).unwrap().result_text, node.result_text);
    let extra = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"rollback"}),
        None,
        json!({}),
    )
    .unwrap();
    let orphan = Node::make(
        NodeKind::Note,
        Some(&"0".repeat(64)),
        0,
        json!({}),
        None,
        json!({}),
    )
    .unwrap();
    assert!(a.apply_batch(vec![extra.clone(), orphan], vec![]).is_err());
    assert!(!a.try_exists(&extra.id).unwrap());
    assert!(db.store("empty").roots().unwrap().is_empty());
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password"]
fn exact_decimal_reservations_retry_expiry_and_tombstones() {
    let db = Database::open();
    let store = db.store("a");
    let b = budget();
    assert!(
        store
            .reserve("a", std::slice::from_ref(&b), &[], 30.0)
            .unwrap()
            .ok
    );
    assert!(
        store
            .reserve("a", std::slice::from_ref(&b), &[], 30.0)
            .unwrap()
            .ok
    );
    let charge = BTreeMap::from([("usd".into(), Decimal::new(1, 1))]);
    store.settle("a", &charge).unwrap();
    store.settle("a", &charge).unwrap();
    assert!(store.settle("a", &BTreeMap::new()).is_err());
    assert!(
        store
            .reserve("b", std::slice::from_ref(&b), &[], 30.0)
            .unwrap()
            .ok
    );
    store.release("b").unwrap();
    assert!(store
        .reserve("b", std::slice::from_ref(&b), &[], 30.0)
        .is_err());
    let mut b = b;
    b.estimates.insert("usd".into(), Decimal::new(2, 1));
    assert!(
        store
            .reserve("c", std::slice::from_ref(&b), &[], 0.01)
            .unwrap()
            .ok
    );
    std::thread::sleep(std::time::Duration::from_millis(20));
    assert!(!store.renew("c", 1.0).unwrap());
    store
        .settle("c", &BTreeMap::from([("usd".into(), Decimal::new(2, 1))]))
        .unwrap();
    assert!(!store.reserve("d", &[b], &[], 30.0).unwrap().ok);
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password"]
fn independent_routed_connections_contend_on_one_window() {
    let db = Database::open();
    db.store("a");
    let gate = Arc::new(Barrier::new(8));
    let jobs: Vec<_> = (0..8)
        .map(|i| {
            let (uri, user, password, o, g) = (
                db.uri.clone(),
                db.user.clone(),
                db.password.clone(),
                db.options("a"),
                gate.clone(),
            );
            std::thread::spawn(move || {
                let s = Neo4jStore::connect_with_options(&uri, &user, &password, o).unwrap();
                g.wait();
                s.reserve(
                    &format!("r{i}"),
                    &[],
                    &[WindowReservation {
                        ledger_key: "w".into(),
                        meter: "requests".into(),
                        limit: 3.into(),
                        amount: Decimal::ONE,
                        window_seconds: 60.0,
                    }],
                    30.0,
                )
                .unwrap()
                .ok
            })
        })
        .collect();
    assert_eq!(
        jobs.into_iter()
            .map(|j| j.join().unwrap() as usize)
            .sum::<usize>(),
        3
    );
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password"]
fn readonly_corrupt_identity_and_coordinator_fail_closed() {
    let db = Database::open();
    let mut o = db.options("missing");
    o.create = false;
    assert!(Neo4jStore::connect_with_options(&db.uri, &db.user, &db.password, o).is_err());
    let mut store = db.store("a");
    let r = root();
    store.put(r.clone()).unwrap();
    let mut o = db.options("a");
    o.read_only = true;
    let mut ro = Neo4jStore::connect_with_options(&db.uri, &db.user, &db.password, o).unwrap();
    assert_eq!(ro.roots().unwrap().len(), 1);
    assert!(ro.put(root()).is_err());
    db.query("MATCH (r:_PollardKV {store_id:$store,bucket:'nodes'}) SET r.item_key='wrong'");
    assert!(matches!(store.get(&r.id), Err(Error::Integrity(_))));
    db.query("MATCH (c:_PollardCoordinator {store_id:$store}) DELETE c");
    assert!(store.reserve("x", &[budget()], &[], 30.0).is_err());
    assert!(store.reconnect().is_err());
    assert!(
        Neo4jStore::connect_with_options(&db.uri, &db.user, &db.password, db.options("a")).is_err()
    );
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password; run serially"]
fn missing_constraint_is_never_silently_repaired() {
    let db = Database::open();
    let store = db.store("a");
    db.query("DROP CONSTRAINT pollard_neo4j_kv_record_key");
    let reconnect = store.reconnect();
    let open =
        Neo4jStore::connect_with_options(&db.uri, &db.user, &db.password, db.options("fresh"));
    db.query("CREATE CONSTRAINT pollard_neo4j_kv_record_key FOR (n:_PollardKV) REQUIRE n.record_key IS UNIQUE");
    assert!(reconnect.is_err());
    assert!(open.is_err());
}
#[test]
#[ignore = "requires isolated Neo4j: POLLARD_TEST_NEO4J_URI and password"]
fn runtime_shared_budget_and_replay_never_redispatch() {
    let db = Database::open();
    let budget = Some(Budget {
        steps: Some(1),
        ..Default::default()
    });
    let rt = Runtime::new(db.store("a"), ReplayMode::Record);
    let mut run = rt.run("shared", budget.clone(), 0).unwrap();
    let node = run
        .model_call(json!({"x":1}), CallOptions::default(), |_| {
            Ok(json!({"text":"done"}))
        })
        .unwrap();
    let other = Runtime::new(db.store("a"), ReplayMode::Record);
    let mut other = other.run("shared", budget.clone(), 0).unwrap();
    assert!(matches!(
        other.model_call(json!({"x":2}), CallOptions::default(), |_| panic!(
            "over budget"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
    let replay = Runtime::new(db.store("a"), ReplayMode::Replay);
    let mut run = replay.run("shared", budget, 0).unwrap();
    assert_eq!(
        run.model_call(json!({"x":1}), CallOptions::default(), |_| panic!("replay"))
            .unwrap()
            .result,
        node.result
    );
}
