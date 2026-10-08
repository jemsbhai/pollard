//! Cache speed must not turn external SQL changes into trusted recordings.
use pollardai::*;
use std::{
    cell::Cell,
    path::PathBuf,
    rc::Rc,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Database(PathBuf);
impl Database {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "pollard-cache-{}-{}.db",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        )))
    }
    fn sql(&self, sql: &str, id: &str) {
        rusqlite::Connection::open(&self.0)
            .unwrap()
            .execute(sql, [id])
            .unwrap();
    }
}
impl Drop for Database {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.0.display()));
        }
    }
}
fn payload(index: usize) -> Value {
    json!({"index":index})
}
fn setup(db: &Database, n: usize) -> (String, Vec<String>) {
    let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Record);
    let mut run = runtime.run("cache", None, 0).unwrap();
    let ids = (0..n)
        .map(|i| {
            run.model_call(payload(i), CallOptions::default(), |_| {
                Ok(json!({"ok":true}))
            })
            .unwrap()
            .id
        })
        .collect();
    (run.root_id().to_owned(), ids)
}
type Hook = Rc<dyn Fn(&str)>;
struct ObservedStore {
    store: SQLiteStore,
    reads: Rc<Cell<usize>>,
    after_get: Option<Hook>,
    after_patch: Option<Hook>,
    cache_available: Rc<Cell<bool>>,
}
impl ObservedStore {
    fn new(db: &Database) -> Self {
        Self {
            store: SQLiteStore::open(&db.0).unwrap(),
            reads: Rc::new(Cell::new(0)),
            after_get: None,
            after_patch: None,
            cache_available: Rc::new(Cell::new(true)),
        }
    }
}
impl Store for ObservedStore {
    fn get(&self, id: &str) -> Result<Node> {
        self.reads.set(self.reads.get() + 1);
        let node = self.store.get(id)?;
        if let Some(hook) = &self.after_get {
            hook(id);
        }
        Ok(node)
    }
    fn put(&mut self, node: Node) -> Result<()> {
        self.store.put(node)
    }
    fn exists(&self, id: &str) -> bool {
        self.store.exists(id)
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        self.store.try_exists(id)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.store.roots()
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.store.children(id)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.store.update_meta(id, patch)?;
        if let Some(hook) = &self.after_patch {
            hook(id);
        }
        Ok(())
    }
}
impl RecordingStore for ObservedStore {
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.store.finalize(node)
    }
    fn cache_revision(&self) -> Option<StoreRevision> {
        self.cache_available
            .get()
            .then(|| self.store.cache_revision())
            .flatten()
    }
}

#[test]
fn hybrid_and_read_only_replay_extend_verified_ancestry_with_linear_reads() {
    let db = Database::new();
    setup(&db, 80);
    for mode in [ReplayMode::Hybrid, ReplayMode::Replay] {
        let mut store = ObservedStore::new(&db);
        if mode == ReplayMode::Replay {
            store.store = SQLiteStore::open_read_only(&db.0).unwrap();
        }
        let reads = store.reads.clone();
        let runtime = Runtime::new(store, mode);
        let mut run = runtime.run("cache", None, 0).unwrap();
        for i in 0..80 {
            run.model_call(payload(i), CallOptions::default(), |_| {
                panic!("replay dispatched")
            })
            .unwrap();
        }
        assert!(
            reads.get() < 80 * 7,
            "{mode:?} repeated ancestor reads: {}",
            reads.get()
        );
        assert_eq!(run.report().unwrap().spent["steps"], 80.0);
        let after_report = reads.get();
        assert_eq!(run.report().unwrap().spent["steps"], 80.0);
        assert_eq!(
            reads.get(),
            after_report,
            "unchanged accounting rescanned the tree"
        );
    }
}

#[test]
fn external_ancestor_and_result_tampering_invalidate_both_replay_modes() {
    for mode in [ReplayMode::Hybrid, ReplayMode::Replay] {
        for column in ["payload", "result"] {
            let db = Database::new();
            let (root, ids) = setup(&db, 2);
            let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), mode);
            let mut run = runtime.run("cache", None, 0).unwrap();
            run.model_call(payload(0), CallOptions::default(), |_| panic!("dispatch"))
                .unwrap();
            if column == "payload" {
                db.sql("UPDATE nodes SET payload='{}' WHERE id=?1", &root);
            } else {
                db.sql("UPDATE nodes SET result='{}' WHERE id=?1", &ids[0]);
            }
            assert!(
                run.model_call(payload(1), CallOptions::default(), |_| panic!(
                    "tampered recording dispatched"
                ))
                .is_err(),
                "{mode:?} accepted changed {column}"
            );
        }
    }
}

#[test]
fn external_metadata_and_sibling_insertions_invalidate_accounting() {
    let db = Database::new();
    let (root, ids) = setup(&db, 2);
    let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Record);
    let run = runtime.run("cache", None, 0).unwrap();
    assert_eq!(run.report().unwrap().spent["steps"], 2.0);
    db.sql(
        "UPDATE nodes SET meta='{\"charges\":{\"steps\":7}}' WHERE id=?1",
        &ids[0],
    );
    assert_eq!(run.report().unwrap().spent["steps"], 8.0);
    let mut writer = SQLiteStore::open(&db.0).unwrap();
    writer
        .put(
            Node::make(
                NodeKind::ModelCall,
                Some(&root),
                9,
                json!({"sibling":true}),
                Some(json!({"ok":true})),
                json!({"charges":{"steps":3}}),
            )
            .unwrap(),
        )
        .unwrap();
    assert_eq!(run.report().unwrap().spent["steps"], 11.0);
    assert_eq!(run.report().unwrap().spent["steps"], 11.0);
}

#[test]
fn deletion_of_cached_ancestor_is_not_hidden() {
    let db = Database::new();
    let (_, ids) = setup(&db, 2);
    let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Hybrid);
    let mut run = runtime.run("cache", None, 0).unwrap();
    run.model_call(payload(0), CallOptions::default(), |_| panic!("dispatch"))
        .unwrap();
    db.sql("DELETE FROM nodes WHERE id=?1", &ids[0]);
    assert!(run
        .model_call(payload(1), CallOptions::default(), |_| panic!("dispatch"))
        .is_err());
}

#[test]
fn cached_interned_payload_is_rechecked_after_external_blob_mutation() {
    let db = Database::new();
    let payload = json!({"body":"long".repeat(400)});
    let recording = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Record);
    let mut run = recording.run("blob", None, 0).unwrap();
    run.model_call(payload.clone(), CallOptions::default(), |_| Ok(json!({})))
        .unwrap();
    run.model_call(json!({"next":true}), CallOptions::default(), |_| {
        Ok(json!({}))
    })
    .unwrap();
    let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Hybrid);
    let mut run = runtime.run("blob", None, 0).unwrap();
    run.model_call(payload, CallOptions::default(), |_| panic!("dispatch"))
        .unwrap();
    rusqlite::Connection::open(&db.0)
        .unwrap()
        .execute("UPDATE blobs SET value='changed'", [])
        .unwrap();
    assert!(run
        .model_call(json!({"next":true}), CallOptions::default(), |_| panic!(
            "dispatch"
        ))
        .is_err());
}

fn mutate_once(db: &Database, target_get: String, changed_node: String, sql: &'static str) -> Hook {
    let fired = Cell::new(false);
    let path = db.0.clone();
    Rc::new(move |id| {
        if id == target_get && !fired.replace(true) {
            rusqlite::Connection::open(&path)
                .unwrap()
                .execute(sql, [&changed_node])
                .unwrap();
        }
    })
}

#[test]
fn commit_during_verification_cannot_reuse_a_stale_prefix() {
    let db = Database::new();
    let (root, ids) = setup(&db, 2);
    let mut store = ObservedStore::new(&db);
    store.after_get = Some(mutate_once(
        &db,
        ids[0].clone(),
        root,
        "UPDATE nodes SET payload='{}' WHERE id=?1",
    ));
    let runtime = Runtime::new(store, ReplayMode::Hybrid);
    let mut run = runtime.run("cache", None, 0).unwrap();
    assert!(run
        .model_call(payload(0), CallOptions::default(), |_| panic!("dispatch"))
        .is_err());
}

#[test]
fn commit_during_local_patch_cannot_be_promoted_to_a_trusted_revision() {
    let db = Database::new();
    let (root, ids) = setup(&db, 2);
    let mut store = ObservedStore::new(&db);
    store.after_patch = Some(mutate_once(
        &db,
        ids[0].clone(),
        root,
        "UPDATE nodes SET payload='{}' WHERE id=?1",
    ));
    let runtime = Runtime::new(store, ReplayMode::Hybrid);
    let mut run = runtime.run("cache", None, 0).unwrap();
    run.model_call(payload(0), CallOptions::default(), |_| panic!("dispatch"))
        .unwrap();
    assert!(run
        .model_call(payload(1), CallOptions::default(), |_| panic!("dispatch"))
        .is_err());
}

#[test]
fn accounting_retries_a_scan_crossing_external_commits() {
    let db = Database::new();
    let (root, ids) = setup(&db, 2);
    let mut store = ObservedStore::new(&db);
    store.after_get = Some(mutate_once(
        &db,
        ids[0].clone(),
        root,
        "UPDATE nodes SET meta='{\"charges\":{\"steps\":5}}' WHERE id=?1",
    ));
    let runtime = Runtime::new(store, ReplayMode::Replay);
    let run = runtime.run("cache", None, 0).unwrap();
    assert_eq!(run.report().unwrap().spent["steps"], 7.0);
}

#[test]
fn triggers_disable_incremental_cache_and_uncommitted_changes_do_not() {
    let db = Database::new();
    let (root, _) = setup(&db, 1);
    let store = SQLiteStore::open(&db.0).unwrap();
    let before = store.cache_revision().unwrap();
    let connection = rusqlite::Connection::open(&db.0).unwrap();
    connection.execute_batch("BEGIN IMMEDIATE").unwrap();
    connection
        .execute("UPDATE nodes SET payload='{}' WHERE id=?1", [&root])
        .unwrap();
    assert_eq!(store.cache_revision(), Some(before));
    connection.execute_batch("ROLLBACK").unwrap();
    assert_eq!(store.cache_revision(), Some(before));
    connection
        .execute_batch(
            "CREATE TRIGGER arbitrary_change AFTER UPDATE ON nodes BEGIN UPDATE kv SET v=v; END;",
        )
        .unwrap();
    assert_eq!(store.cache_revision(), None);
    connection
        .execute_batch("DROP TRIGGER arbitrary_change")
        .unwrap();
    assert_ne!(store.cache_revision(), Some(before));
    assert!(store.cache_revision().is_some());
}

#[test]
fn temporarily_unavailable_token_discards_all_cached_accounting() {
    let db = Database::new();
    let (_, ids) = setup(&db, 1);
    let store = ObservedStore::new(&db);
    let available = store.cache_available.clone();
    let reads = store.reads.clone();
    let runtime = Runtime::new(store, ReplayMode::Replay);
    let run = runtime.run("cache", None, 0).unwrap();
    assert_eq!(run.report().unwrap().spent["steps"], 1.0);
    available.set(false);
    db.sql(
        "UPDATE nodes SET meta='{\"charges\":{\"steps\":9}}' WHERE id=?1",
        &ids[0],
    );
    assert_eq!(run.report().unwrap().spent["steps"], 9.0);
    let uncached = reads.get();
    available.set(true);
    assert_eq!(run.report().unwrap().spent["steps"], 9.0);
    assert!(
        reads.get() > uncached,
        "missing token retained an old cache"
    );
}

#[test]
fn externally_replaced_view_disables_cache_even_when_rows_are_valid() {
    let db = Database::new();
    setup(&db, 1);
    let store = SQLiteStore::open(&db.0).unwrap();
    assert!(store.cache_revision().is_some());
    rusqlite::Connection::open(&db.0).unwrap().execute_batch(
        "ALTER TABLE nodes RENAME TO stored_nodes; CREATE VIEW nodes AS SELECT * FROM stored_nodes;"
    ).unwrap();
    assert_eq!(store.cache_revision(), None);
    let runtime = Runtime::new(store, ReplayMode::Replay);
    let mut run = runtime.run("cache", None, 0).unwrap();
    run.model_call(payload(0), CallOptions::default(), |_| panic!("dispatch"))
        .unwrap();
}

#[test]
fn failed_write_invalidates_local_token_without_changing_records() {
    let db = Database::new();
    let (root, _) = setup(&db, 1);
    let mut store = SQLiteStore::open(&db.0).unwrap();
    let before = store.cache_revision().unwrap();
    let orphan = Node::make(
        NodeKind::Note,
        Some(&"f".repeat(64)),
        0,
        json!({"orphan":true}),
        None,
        json!({}),
    )
    .unwrap();
    assert!(store.put(orphan).is_err());
    let after = store.cache_revision().unwrap();
    assert_ne!(before.local, after.local);
    assert_eq!(before.external, after.external);
    assert_eq!(store.walk(&root).unwrap().len(), 2);
    assert!(verify_subtree(&store, &root).ok);
}
