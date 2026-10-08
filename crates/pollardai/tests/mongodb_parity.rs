#![cfg(feature = "mongodb")]
use mongodb::{bson::doc, sync::Client};
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
    name: String,
    client: Client,
}
impl Database {
    fn open() -> Option<Self> {
        let uri =
            std::env::var("POLLARD_TEST_MONGO_URI").expect("POLLARD_TEST_MONGO_URI is required");
        let name = format!(
            "pollard_rust_{}_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        Some(Self {
            client: Client::with_uri_str(&uri).unwrap(),
            uri,
            name,
        })
    }
    fn options(&self, id: &str) -> MongoOptions {
        MongoOptions {
            database: self.name.clone(),
            store_id: id.into(),
            ..Default::default()
        }
    }
    fn store(&self, id: &str) -> MongoStore {
        MongoStore::connect_with_options(&self.uri, self.options(id)).unwrap()
    }
}
impl Drop for Database {
    fn drop(&mut self) {
        let _ = self.client.database(&self.name).drop(None);
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
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn native_manifest_seal_reopen_and_failed_batch_are_atomic() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut a = db.store("a");
    let root = root();
    a.put(root.clone()).unwrap();
    let call = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"p":"é東京","big":12345678901234567890u64}),
        Some(json!({"float":1e-5,"negative":-0.0})),
        json!({}),
    )
    .unwrap();
    a.put(call.clone()).unwrap();
    assert_eq!(a.get(&call.id).unwrap(), call);
    assert_eq!(
        a.children(&root.id).unwrap(),
        std::slice::from_ref(&call.id)
    );
    assert!(verify_subtree(&a, &root.id).ok);
    let mut copy = db.store("copy");
    import_manifest(export_manifest(&a, &root.id).unwrap(), &mut copy).unwrap();
    assert_eq!(seal(&a, &root.id).unwrap(), seal(&copy, &root.id).unwrap());
    a.reconnect().unwrap();
    assert_eq!(a.get(&call.id).unwrap(), call);
    let mut orphan = call.clone();
    orphan.parent = Some("missing".into());
    let extra = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"rollback"}),
        None,
        json!({}),
    )
    .unwrap();
    assert!(a
        .apply_batch(vec![extra.clone(), orphan], Vec::new())
        .is_err());
    assert!(!a.try_exists(&extra.id).unwrap());
    assert!(db.store("empty").roots().unwrap().is_empty());
}
#[test]
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn exact_decimal_reservations_retry_expiry_and_permanent_tombstones() {
    let Some(db) = Database::open() else {
        return;
    };
    let store = db.store("ledger");
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
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn independent_connections_contend_on_one_window() {
    let Some(db) = Database::open() else {
        return;
    };
    db.store("window");
    let gate = Arc::new(Barrier::new(8));
    let jobs: Vec<_> = (0..8)
        .map(|i| {
            let (uri, o, g) = (db.uri.clone(), db.options("window"), gate.clone());
            std::thread::spawn(move || {
                let s = MongoStore::connect_with_options(&uri, o).unwrap();
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
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn readonly_and_reconnect_never_repair_corrupt_schema() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut o = db.options("missing");
    o.create = false;
    assert!(MongoStore::connect_with_options(&db.uri, o).is_err());
    assert!(db
        .client
        .database(&db.name)
        .list_collection_names(None)
        .unwrap()
        .is_empty());
    let mut store = db.store("existing");
    store.put(root()).unwrap();
    let mut o = db.options("existing");
    o.read_only = true;
    let mut ro = MongoStore::connect_with_options(&db.uri, o).unwrap();
    assert_eq!(ro.roots().unwrap().len(), 1);
    assert!(ro.put(root()).is_err());
    let records = db
        .client
        .database(&db.name)
        .collection::<mongodb::bson::Document>("pollard_records");
    records
        .drop_index("pollard_store_bucket_key_unique", None)
        .unwrap();
    assert!(store.reconnect().is_err());
    assert!(MongoStore::connect_with_options(&db.uri, db.options("existing")).is_err());
    assert_eq!(records.list_index_names().unwrap(), ["_id_"]);
}
#[test]
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn corrupt_identity_coordinator_and_sparse_indexes_fail_closed() {
    let Some(db) = Database::open() else {
        return;
    };
    let mut store = db.store("corrupt");
    let r = root();
    store.put(r.clone()).unwrap();
    let records = db
        .client
        .database(&db.name)
        .collection::<mongodb::bson::Document>("pollard_records");
    records
        .update_one(
            doc! {"store_id":"corrupt","bucket":"nodes","key":&r.id},
            doc! {"$set":{"key":"wrong"}},
            None,
        )
        .unwrap();
    assert!(matches!(store.get(&r.id), Err(Error::Integrity(_))));
    let coord = db
        .client
        .database(&db.name)
        .collection::<mongodb::bson::Document>("pollard_coordinators");
    coord.delete_one(doc! {"_id":"corrupt"}, None).unwrap();
    assert!(store.reserve("x", &[budget()], &[], 30.0).is_err());
    assert_eq!(
        coord.count_documents(doc! {"_id":"corrupt"}, None).unwrap(),
        0
    );
    records
        .drop_index("pollard_store_bucket_key_unique", None)
        .unwrap();
    records
        .create_index(
            mongodb::IndexModel::builder()
                .keys(doc! {"store_id":1,"bucket":1,"key":1})
                .options(
                    mongodb::options::IndexOptions::builder()
                        .unique(true)
                        .sparse(true)
                        .build(),
                )
                .build(),
            None,
        )
        .unwrap();
    assert!(MongoStore::connect_with_options(&db.uri, db.options("fresh")).is_err());
}
#[test]
#[ignore = "requires isolated MongoDB: POLLARD_TEST_MONGO_URI"]
fn sync_store_is_safe_inside_an_existing_async_runtime() {
    let Some(db) = Database::open() else {
        return;
    };
    let options = db.options("async");
    let uri = db.uri.clone();
    let runtime = tokio_runtime();
    runtime.block_on(async move {
        let store = MongoStore::connect_with_options(&uri, options).unwrap();
        let rt = AsyncRuntime::new(store, ReplayMode::Record);
        let mut run = rt.run("async", None, 0).unwrap();
        let value = run
            .amodel_call(json!({"x":1}), CallOptions::default(), |_| async {
                Ok(json!({"text":"ok"}))
            })
            .await
            .unwrap();
        assert_eq!(value.result.unwrap()["text"], "ok");
    });
}
fn tokio_runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}
