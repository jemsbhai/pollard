use pollardai::*;
use rusqlite::{params, Connection};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        Self::at_time(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        )
    }
    fn at_time(timestamp: u128) -> Self {
        let filename = format!(
            "pollard-legacy-test-{}-{}-{}.db",
            std::process::id(),
            timestamp,
            NEXT_TEMP.fetch_add(1, Ordering::Relaxed),
        );
        Self(std::env::temp_dir().join(filename))
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.0.display()));
        }
    }
}

#[test]
fn temporary_paths_are_unique_when_parallel_allocations_share_a_clock_tick() {
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let workers: Vec<_> = (0..8)
        .map(|_| {
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                (0..128).map(|_| Temp::at_time(0)).collect::<Vec<_>>()
            })
        })
        .collect();
    let paths: Vec<_> = workers
        .into_iter()
        .flat_map(|worker| worker.join().unwrap())
        .collect();
    let unique: std::collections::BTreeSet<_> = paths.iter().map(|path| &path.0).collect();
    assert_eq!(unique.len(), paths.len());
    assert_eq!(paths.len(), 1024);
}

fn legacy(version: Option<&str>, tamper: bool) -> (Temp, Node) {
    let path = Temp::new();
    let conn = Connection::open(&path.0).unwrap();
    conn.execute_batch("CREATE TABLE nodes(id TEXT PRIMARY KEY,parent TEXT,kind TEXT NOT NULL,attempt INTEGER NOT NULL,payload TEXT NOT NULL,result TEXT,result_digest TEXT,meta TEXT NOT NULL);CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);").unwrap();
    if let Some(v) = version {
        conn.execute("INSERT INTO kv VALUES('schema_version',?1)", [v])
            .unwrap();
    }
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"legacy","literal":{"__pollard_ref":"0".repeat(64)}}),
        None,
        json!({}),
    )
    .unwrap();
    conn.execute(
        "INSERT INTO nodes VALUES(?1,NULL,'root',0,?2,NULL,NULL,'{}')",
        params![
            if tamper {
                "0".repeat(64)
            } else {
                root.id.clone()
            },
            String::from_utf8(canonical_bytes(&root.payload).unwrap()).unwrap()
        ],
    )
    .unwrap();
    (path, root)
}
#[test]
fn explicit_migration_preserves_schema_zero_and_one_literal_identity() {
    for version in [None, Some("0"), Some("1")] {
        let (path, root) = legacy(version, false);
        assert!(SQLiteStore::open_read_only(&path.0).is_err());
        assert!(SQLiteStore::open(&path.0).is_err());
        let store = SQLiteStore::migrate_legacy(&path.0).unwrap();
        assert_eq!(store.get(&root.id).unwrap(), root);
        assert!(verify(&store, &root.id).ok);
        drop(store);
        let reopened = SQLiteStore::open_read_only(&path.0).unwrap();
        assert_eq!(reopened.get(&root.id).unwrap(), root);
    }
}
#[test]
fn schema_two_migration_preserves_interned_payloads_and_exact_results() {
    let path = Temp::new();
    let mut store = SQLiteStore::open_with_options(&path.0, false, Some(8)).unwrap();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"migration two"}),
        None,
        json!({}),
    )
    .unwrap();
    store.put(root.clone()).unwrap();
    let child = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"prompt":"long string to intern","literal":{"__pollard_ref":"a".repeat(64)}}),
        Some(json!({"value":1e-7})),
        json!({}),
    )
    .unwrap();
    store.put(child.clone()).unwrap();
    drop(store);
    let conn = Connection::open(&path.0).unwrap();
    conn.execute_batch("UPDATE kv SET v='2' WHERE k='schema_version';DROP TABLE budget_state;DROP TABLE reservations;DROP TABLE window_events;").unwrap();
    drop(conn);
    let store = SQLiteStore::migrate_legacy(&path.0).unwrap();
    assert_eq!(store.get(&root.id).unwrap(), root);
    assert_eq!(store.get(&child.id).unwrap(), child);
}
#[test]
fn migration_errors_roll_back_schema_and_never_create_a_missing_file() {
    let absent = Temp::new();
    assert!(SQLiteStore::migrate_legacy(&absent.0).is_err());
    assert!(!absent.0.exists());
    for (version, tamper) in [("1", true), ("999", false), ("bad", false)] {
        let (path, _) = legacy(Some(version), tamper);
        assert!(SQLiteStore::migrate_legacy(&path.0).is_err());
        let conn = Connection::open(&path.0).unwrap();
        assert_eq!(
            conn.query_row("SELECT v FROM kv WHERE k='schema_version'", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            version
        );
        assert_eq!(
            conn.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table'",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
            2
        );
    }
}
