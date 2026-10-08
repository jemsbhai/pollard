use pollardai::*;
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Database(PathBuf);
impl Database {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "pollard-rust-{}-{}.db",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        )))
    }
}
impl Drop for Database {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.0.display()));
        }
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
fn child(parent: &Node, kind: NodeKind, attempt: u64, result: Option<Value>) -> Node {
    Node::make(
        kind,
        Some(&parent.id),
        attempt,
        json!({"hello":"world"}),
        result,
        json!({}),
    )
    .unwrap()
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("python_first_run.json")).unwrap()
}

#[test]
fn python_160_manifest_and_seal_are_exact() {
    let mut store = MemoryStore::new();
    let manifest = fixture();
    let report = import_manifest(manifest.clone(), &mut store).unwrap();
    assert_eq!(report.imported, 2);
    assert_eq!(
        report.digest,
        "33bd9249f8166ca470553dfada515f44a9f0ce24b7c615dad48e0a89a5936ee3"
    );
    assert_eq!(
        serde_json::to_value(seal(&store, &report.root_id).unwrap()).unwrap(),
        manifest["seal"]
    );
    assert_eq!(export_manifest(&store, &report.root_id).unwrap(), manifest);
    let repeated = import_manifest(fixture(), &mut store).unwrap();
    assert_eq!((repeated.imported, repeated.existing), (0, 2));
}

#[test]
fn sqlite_reopen_preserves_python_manifest_and_read_only_does_not_write() {
    let db = Database::new();
    let report;
    {
        let mut store = SQLiteStore::open(db.0.as_path()).unwrap();
        report = import_manifest(fixture(), &mut store).unwrap();
    }
    let before = std::fs::read(db.0.as_path()).unwrap();
    {
        let mut store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
        assert_eq!(export_manifest(&store, &report.root_id).unwrap(), fixture());
        assert!(store.put(root("cannot-write")).is_err());
        assert!(store.update_meta(&report.root_id, json!({"x":1})).is_err());
        assert!(store.drop_nodes(&BTreeSet::new()).is_err());
    }
    assert_eq!(before, std::fs::read(db.0.as_path()).unwrap());
    let missing = Database::new();
    assert!(SQLiteStore::open_read_only(&missing.0).is_err());
    assert!(!missing.0.exists());
}

#[test]
fn sqlite_rejects_unknown_schema_without_repair() {
    let db = Database::new();
    drop(SQLiteStore::open(db.0.as_path()).unwrap());
    {
        let conn = rusqlite::Connection::open(db.0.as_path()).unwrap();
        conn.execute("UPDATE kv SET v='999' WHERE k='schema_version'", [])
            .unwrap();
    }
    let before = std::fs::read(db.0.as_path()).unwrap();
    assert!(SQLiteStore::open(db.0.as_path()).is_err());
    assert!(SQLiteStore::open_read_only(db.0.as_path()).is_err());
    assert_eq!(before, std::fs::read(db.0.as_path()).unwrap());
}

#[test]
fn sqlite_refuses_incomplete_arbiter_schema_without_recreating_tables() {
    let db = Database::new();
    drop(SQLiteStore::open(db.0.as_path()).unwrap());
    {
        let connection = rusqlite::Connection::open(db.0.as_path()).unwrap();
        connection.execute("DROP TABLE reservations", []).unwrap();
    }
    let before = std::fs::read(db.0.as_path()).unwrap();
    assert!(SQLiteStore::open(db.0.as_path()).is_err());
    assert_eq!(before, std::fs::read(db.0.as_path()).unwrap());
    // Python's read-only schema contract does not require arbitration tables.
    assert!(SQLiteStore::open_read_only(db.0.as_path()).is_ok());
}

#[test]
fn sqlite_interning_distinguishes_literal_references_and_unicode_paths() {
    let db = Database::new();
    let mut store = SQLiteStore::open_with_options(db.0.as_path(), false, Some(4)).unwrap();
    let value = json!({"run":"test","nested":{"é":["long string",{"__pollard_ref":"a".repeat(64)}]},"literal":{"__pollard_ref":"b".repeat(64)}});
    let node = Node::make(NodeKind::Root, None, 0, value.clone(), None, json!({})).unwrap();
    store.put(node.clone()).unwrap();
    assert_eq!(store.get(&node.id).unwrap().payload, value);
    drop(store);
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    assert_eq!(store.get(&node.id).unwrap(), node);
    let connection = rusqlite::Connection::open(db.0.as_path()).unwrap();
    let count: u64 = connection
        .query_row("SELECT count(*) FROM blobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 2);
    let literals: u64 = connection
        .query_row("SELECT count(*) FROM blob_literals", [], |r| r.get(0))
        .unwrap();
    assert_eq!(literals, 2);
}

#[test]
fn corrupted_interned_blob_fails_closed() {
    let db = Database::new();
    let id;
    {
        let mut store = SQLiteStore::open_with_options(db.0.as_path(), false, Some(2)).unwrap();
        let node = root("interned");
        id = node.id.clone();
        store.put(node).unwrap();
    }
    {
        let conn = rusqlite::Connection::open(db.0.as_path()).unwrap();
        conn.execute("UPDATE blobs SET value='tampered'", [])
            .unwrap();
    }
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    assert!(store.get(&id).is_err());
    assert!(!verify(&store, &id).ok);
}

#[test]
fn sqlite_result_integrity_checks_exact_stored_bytes() {
    let db = Database::new();
    let id;
    {
        let mut store = SQLiteStore::open(db.0.as_path()).unwrap();
        let report = import_manifest(fixture(), &mut store).unwrap();
        id = store.children(&report.root_id).unwrap()[0].clone();
    }
    {
        let conn = rusqlite::Connection::open(db.0.as_path()).unwrap();
        conn.execute("UPDATE nodes SET result='{}' WHERE id=?1", [&id])
            .unwrap();
    }
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    assert!(!verify(&store, &id).ok);
}

#[test]
fn subtree_verification_detects_bad_descendant_while_ancestry_remains_valid() {
    let db = Database::new();
    let root;
    {
        let mut store = SQLiteStore::open(db.0.as_path()).unwrap();
        root = import_manifest(fixture(), &mut store).unwrap().root_id;
    }
    {
        let conn = rusqlite::Connection::open(db.0.as_path()).unwrap();
        conn.execute("UPDATE nodes SET result='{}' WHERE parent IS NOT NULL", [])
            .unwrap();
    }
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    assert!(verify(&store, &root).ok);
    assert!(!verify_subtree(&store, &root).ok);
}

#[test]
fn merge_metadata_list_union_deduplicates_different_object_key_orders() {
    let mut left = MemoryStore::new();
    let mut right = MemoryStore::new();
    let r = root("key-order");
    left.put(r.clone()).unwrap();
    right.put(r.clone()).unwrap();
    left.update_meta(
        &r.id,
        serde_json::from_str(r#"{"items":[{"a":1,"b":2}]}"#).unwrap(),
    )
    .unwrap();
    right
        .update_meta(
            &r.id,
            serde_json::from_str(r#"{"items":[{"b":2,"a":1}]}"#).unwrap(),
        )
        .unwrap();
    merge(&mut left, &right, false).unwrap();
    assert_eq!(
        left.get(&r.id).unwrap().meta["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

fn pending_settlement(store: &mut dyn RecordingStore) {
    let r = root("pending");
    store.put(r.clone()).unwrap();
    let pending = Node::make(
        NodeKind::ModelCall,
        Some(&r.id),
        0,
        json!({}),
        None,
        json!({"state":"pending","charges":{"steps":1}}),
    )
    .unwrap();
    store.put(pending.clone()).unwrap();
    let completed = Node::make(
        NodeKind::ModelCall,
        Some(&r.id),
        0,
        json!({}),
        Some(json!({"ok":true})),
        json!({"state":"completed","charges":{"steps":1}}),
    )
    .unwrap();
    store.finalize(completed.clone()).unwrap();
    assert_eq!(store.get(&pending.id).unwrap(), completed);
    assert!(store.finalize(completed).is_err());
}
#[test]
fn sqlite_and_memory_settle_pending_once() {
    pending_settlement(&mut MemoryStore::new());
    let db = Database::new();
    pending_settlement(&mut SQLiteStore::open(db.0.as_path()).unwrap());
}

#[test]
fn sqlite_pending_survives_reopen_and_original_result_wins_duplicates() {
    let db = Database::new();
    let r = root("pending-reopen");
    let pending = Node::make(
        NodeKind::ModelCall,
        Some(&r.id),
        0,
        json!({}),
        None,
        json!({"state":"pending"}),
    )
    .unwrap();
    {
        let mut store = SQLiteStore::open(db.0.as_path()).unwrap();
        store.put(r.clone()).unwrap();
        store.put(pending.clone()).unwrap();
    }
    let mut store = SQLiteStore::open(db.0.as_path()).unwrap();
    let first = Node::make(
        NodeKind::ModelCall,
        Some(&r.id),
        0,
        json!({}),
        Some(json!({"value":1})),
        json!({"state":"completed"}),
    )
    .unwrap();
    store.finalize(first.clone()).unwrap();
    let second = Node::make(
        NodeKind::ModelCall,
        Some(&r.id),
        0,
        json!({}),
        Some(json!({"value":2})),
        json!({}),
    )
    .unwrap();
    store.put(second.clone()).unwrap();
    let found = store.get(&pending.id).unwrap();
    assert_eq!(found.result, first.result);
    assert_eq!(
        found.meta["result_conflicts"][0]["result"],
        second.result.unwrap()
    );
}

#[test]
fn atomic_batch_rolls_back_on_late_missing_parent() {
    let db = Database::new();
    let mut stores: Vec<Box<dyn Store>> = vec![
        Box::new(MemoryStore::new()),
        Box::new(SQLiteStore::open(db.0.as_path()).unwrap()),
    ];
    for store in &mut stores {
        let r = root("should-rollback");
        let absent = root("absent");
        let bad = child(&absent, NodeKind::Note, 0, None);
        assert!(store.apply_batch(vec![r.clone(), bad], Vec::new()).is_err());
        assert!(!store.exists(&r.id));
        assert!(store
            .apply_batch(vec![r.clone()], vec![(r.id.clone(), json!(false))])
            .is_err());
        assert!(!store.exists(&r.id));
    }
}

#[test]
fn corrupted_or_duplicate_manifests_do_not_mutate_destination() {
    for mutation in 0..4 {
        let mut manifest = fixture();
        match mutation {
            0 => manifest["nodes"][1]["result"] = json!("{}"),
            1 => manifest["seal"]["digest"] = json!("a".repeat(64)),
            2 => {
                let duplicate = manifest["nodes"][0].clone();
                manifest["nodes"].as_array_mut().unwrap().push(duplicate);
            }
            _ => manifest["nodes"].as_array_mut().unwrap().swap(0, 1),
        }
        let mut store = MemoryStore::new();
        assert!(import_manifest(manifest, &mut store).is_err());
        assert!(store.roots().unwrap().is_empty());
    }
}

#[test]
fn nonroot_subtree_import_requires_external_parent() {
    let mut source = MemoryStore::new();
    let r = root("parent");
    let c = child(&r, NodeKind::Note, 0, None);
    source.put(r.clone()).unwrap();
    source.put(c.clone()).unwrap();
    let manifest = export_manifest(&source, &c.id).unwrap();
    let mut target = MemoryStore::new();
    assert!(import_manifest(manifest.clone(), &mut target).is_err());
    target.put(r).unwrap();
    assert_eq!(import_manifest(manifest, &mut target).unwrap().imported, 1);
}

#[test]
fn import_collision_is_rejected_without_any_partial_new_nodes() {
    let mut target = MemoryStore::new();
    let mut source = MemoryStore::new();
    let r = root("collision");
    target.put(r.clone()).unwrap();
    source.put(r.clone()).unwrap();
    let a = child(&r, NodeKind::ModelCall, 0, Some(json!({"value":1})));
    let b = child(&r, NodeKind::ModelCall, 0, Some(json!({"value":2})));
    source.put(a).unwrap();
    target.put(b).unwrap();
    let note = child(&r, NodeKind::Note, 0, None);
    source.put(note.clone()).unwrap();
    assert!(import_manifest(export_manifest(&source, &r.id).unwrap(), &mut target).is_err());
    assert!(!target.exists(&note.id));
}

#[test]
fn merge_keeps_results_merges_metadata_and_reruns_idempotently() {
    let mut left = MemoryStore::new();
    let mut right = MemoryStore::new();
    let r = root("merge");
    left.put(r.clone()).unwrap();
    right.put(r.clone()).unwrap();
    let mut a = child(&r, NodeKind::ModelCall, 0, Some(json!({"value":1})));
    a.meta = json!({"nested":{"shared":1,"left":true},"tags":["a"]});
    let mut b = child(&r, NodeKind::ModelCall, 0, Some(json!({"value":2})));
    b.meta = json!({"nested":{"shared":2,"right":true},"tags":["b"]});
    left.put(a.clone()).unwrap();
    right.put(b).unwrap();
    let report = merge(&mut left, &right, false).unwrap();
    assert_eq!(
        (
            report.copied,
            report.existing,
            report.result_conflicts,
            report.meta_conflicts
        ),
        (0, 2, 1, 1)
    );
    let got = left.get(&a.id).unwrap();
    assert_eq!(got.result, a.result);
    assert_eq!(got.meta["tags"], json!(["a", "b"]));
    assert_eq!(got.meta["nested"]["right"], json!(true));
    assert_eq!(
        got.meta["merge_conflicts"][0],
        json!({"path":"nested.shared","values":[1,2]})
    );
    let again = merge(&mut left, &right, false).unwrap();
    assert_eq!((again.result_conflicts, again.meta_conflicts), (0, 0));
    assert_eq!(left.get(&a.id).unwrap(), got);
}

#[test]
fn replay_merge_rejects_conflicts_before_copying_any_root() {
    let mut left = MemoryStore::new();
    let mut right = MemoryStore::new();
    let r = root("z-collision");
    left.put(r.clone()).unwrap();
    right.put(root("a-would-copy")).unwrap();
    right.put(r.clone()).unwrap();
    left.put(child(&r, NodeKind::ModelCall, 0, Some(json!({"v":1}))))
        .unwrap();
    right
        .put(child(&r, NodeKind::ModelCall, 0, Some(json!({"v":2}))))
        .unwrap();
    assert!(merge(&mut left, &right, true).is_err());
    assert_eq!(left.roots().unwrap(), vec![r.id]);
}

#[test]
fn child_index_preserves_kind_then_id_order_and_revision() {
    let mut store = MemoryStore::new();
    let before = store.revision();
    let r = root("ordering");
    store.put(r.clone()).unwrap();
    assert_ne!(before, store.revision());
    let mut expected = Vec::new();
    for kind in [
        NodeKind::ToolCall,
        NodeKind::Refusal,
        NodeKind::Note,
        NodeKind::ModelCall,
    ] {
        for attempt in [2, 0, 1] {
            let n = child(&r, kind, attempt, None);
            expected.push((kind.as_str(), n.id.clone()));
            store.put(n).unwrap();
        }
    }
    expected.sort();
    assert_eq!(
        store.children(&r.id).unwrap(),
        expected
            .iter()
            .map(|(_, id)| id.clone())
            .collect::<Vec<_>>()
    );
    let rev = store.revision();
    store.update_meta(&r.id, json!({"updated":true})).unwrap();
    assert_ne!(rev, store.revision());
    assert_eq!(store.walk(&r.id).unwrap().len(), 13);
}

#[test]
fn gc_drops_pruned_descendants_and_compacts_orphaned_blobs() {
    let db = Database::new();
    let mut store = SQLiteStore::open_with_options(db.0.as_path(), false, Some(4)).unwrap();
    let r = root("retained");
    store.put(r.clone()).unwrap();
    let c = child(&r, NodeKind::Note, 0, None);
    store.put(c.clone()).unwrap();
    let d = child(&c, NodeKind::Note, 0, None);
    store.put(d.clone()).unwrap();
    store.update_meta(&c.id, json!({"pruned":true})).unwrap();
    let report = gc(&mut store, "drop-pruned").unwrap();
    assert_eq!(report.removed_nodes, 2);
    assert!(store.exists(&r.id));
    assert!(!store.exists(&d.id));
    assert_eq!(
        report.survivor_seals[&r.id],
        seal(&store, &r.id).unwrap().digest
    );
    let compacted = gc(&mut store, "compact").unwrap();
    assert_eq!(compacted.removed_blobs, 1);
}

#[test]
fn garbage_collection_rejects_orphaning_unselected_children() {
    let mut store = MemoryStore::new();
    let r = root("parent");
    let c = child(&r, NodeKind::Note, 0, None);
    store.put(r.clone()).unwrap();
    store.put(c.clone()).unwrap();
    assert!(store.drop_nodes(&BTreeSet::from([r.id.clone()])).is_err());
    assert!(store.exists(&r.id));
    assert!(store.exists(&c.id));
}

#[test]
fn concurrent_sqlite_merges_preserve_every_metadata_update() {
    let db = Database::new();
    drop(SQLiteStore::open(db.0.as_path()).unwrap());
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let workers: Vec<_> = (0..8)
        .map(|i| {
            let path = db.0.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let mut source = MemoryStore::new();
                let mut r = root("concurrent-merge");
                r.meta = json!({"workers":{i.to_string():true}});
                source.put(r).unwrap();
                let mut target = SQLiteStore::open(path).unwrap();
                barrier.wait();
                merge(&mut target, &source, false).unwrap()
            })
        })
        .collect();
    let copied: usize = workers.into_iter().map(|w| w.join().unwrap().copied).sum();
    assert_eq!(copied, 1);
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    let node = store.get(&root("concurrent-merge").id).unwrap();
    assert_eq!(node.meta["workers"].as_object().unwrap().len(), 8);
}

#[test]
fn conflicting_sqlite_imports_are_strict_under_concurrent_writers() {
    let db = Database::new();
    drop(SQLiteStore::open(db.0.as_path()).unwrap());
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let workers: Vec<_> = (0..2)
        .map(|i| {
            let path = db.0.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let mut source = MemoryStore::new();
                let r = root("concurrent-import");
                source.put(r.clone()).unwrap();
                source
                    .put(child(&r, NodeKind::ModelCall, 0, Some(json!({"choice":i}))))
                    .unwrap();
                let manifest = export_manifest(&source, &r.id).unwrap();
                let mut target = SQLiteStore::open(path).unwrap();
                barrier.wait();
                import_manifest(manifest, &mut target).is_ok()
            })
        })
        .collect();
    assert_eq!(
        workers
            .into_iter()
            .map(|w| usize::from(w.join().unwrap()))
            .sum::<usize>(),
        1
    );
    let store = SQLiteStore::open_read_only(db.0.as_path()).unwrap();
    let nodes = store.walk(&root("concurrent-import").id).unwrap();
    assert_eq!(nodes.len(), 2);
    assert!(nodes[1].meta.get("result_conflicts").is_none());
}
