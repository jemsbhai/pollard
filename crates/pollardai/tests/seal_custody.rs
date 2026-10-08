use pollardai::*;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "pollard-custody-{}-{}.db",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        )))
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
fn report() -> SealReport {
    let mut store = MemoryStore::new();
    let fixture = serde_json::from_str(include_str!("python_first_run.json")).unwrap();
    let imported = import_manifest(fixture, &mut store).unwrap();
    seal(&store, &imported.root_id).unwrap()
}
#[test]
fn custody_is_independent_append_only_and_survives_reopen() {
    let path = Temp::new();
    let report = report();
    let first;
    {
        let mut sink = SQLiteSealSink::open(path.0.as_path()).unwrap();
        first = sink
            .publish(
                &report,
                "prod-ledger",
                "release-signer",
                Some("2026-10-08T12:00:00Z"),
            )
            .unwrap();
        assert_eq!(first.sequence, 1);
    }
    {
        let mut sink = SQLiteSealSink::open(path.0.as_path()).unwrap();
        let second = sink
            .publish(&report, "prod-ledger", "second-signer", None)
            .unwrap();
        assert_eq!(second.sequence, 2);
        assert!(second.sealed_at.ends_with('Z'));
        assert_eq!(sink.records().unwrap(), vec![first, second]);
    }
}
#[test]
fn custody_refuses_store_database_without_creating_sink_tables() {
    let path = Temp::new();
    drop(SQLiteStore::open(path.0.as_path()).unwrap());
    assert!(SQLiteSealSink::open(path.0.as_path()).is_err());
    let connection = rusqlite::Connection::open(path.0.as_path()).unwrap();
    let count: u64 = connection
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'seal_custody_%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}
#[test]
fn invalid_publication_does_not_append() {
    let path = Temp::new();
    let mut sink = SQLiteSealSink::open(path.0.as_path()).unwrap();
    let report = report();
    assert!(sink.publish(&report, "", "signer", None).is_err());
    assert!(sink.publish(&report, "store", "", None).is_err());
    let mut tampered = report.clone();
    tampered.digest = "a".repeat(64);
    assert!(sink.publish(&tampered, "store", "signer", None).is_err());
    let mut tampered = report.clone();
    tampered.entries[1].previous = None;
    assert!(sink.publish(&tampered, "store", "signer", None).is_err());
    let mut tampered = report;
    tampered.entries[0].node_id = "b".repeat(64);
    assert!(sink.publish(&tampered, "store", "signer", None).is_err());
    assert!(sink.records().unwrap().is_empty());
}
#[test]
fn future_custody_schema_refused_without_repair() {
    let path = Temp::new();
    drop(SQLiteSealSink::open(path.0.as_path()).unwrap());
    {
        let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
        c.execute("UPDATE seal_custody_schema SET version=2", [])
            .unwrap();
    }
    let before = std::fs::read(path.0.as_path()).unwrap();
    assert!(SQLiteSealSink::open(path.0.as_path()).is_err());
    assert_eq!(before, std::fs::read(path.0.as_path()).unwrap());
}

#[test]
fn custody_publication_changes_only_the_named_sink_database() {
    let directory = std::env::temp_dir().join(format!(
        "pollard-custody-isolation-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&directory).unwrap();
    let recording = directory.join("recording.db");
    let sink_path = directory.join("custody.db");
    let sentinel = directory.join("sentinel.txt");
    std::fs::write(&sentinel, b"unchanged unrelated data").unwrap();
    let report = {
        let mut store = SQLiteStore::open(&recording).unwrap();
        let imported = import_manifest(
            serde_json::from_str(include_str!("python_first_run.json")).unwrap(),
            &mut store,
        )
        .unwrap();
        seal(&store, &imported.root_id).unwrap()
    };
    let before = std::fs::read(&recording).unwrap();
    {
        let mut sink = SQLiteSealSink::open(&sink_path).unwrap();
        sink.publish(
            &report,
            "named-store",
            "named-signer",
            Some("2026-10-08T12:00:00Z"),
        )
        .unwrap();
        assert_eq!(sink.records().unwrap().len(), 1);
    }
    assert_eq!(before, std::fs::read(&recording).unwrap());
    assert_eq!(
        std::fs::read(&sentinel).unwrap(),
        b"unchanged unrelated data"
    );
    let mut names: Vec<_> = std::fs::read_dir(&directory)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(names, vec!["custody.db", "recording.db", "sentinel.txt"]);
    let published = std::fs::read(&sink_path).unwrap();
    {
        let sink = SQLiteSealSink::open(&sink_path).unwrap();
        assert_eq!(sink.records().unwrap().len(), 1);
    }
    assert_eq!(published, std::fs::read(&sink_path).unwrap());
    for path in [recording, sink_path, sentinel] {
        std::fs::remove_file(path).unwrap();
    }
    std::fs::remove_dir(directory).unwrap();
}
