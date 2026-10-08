//! SQLite record, hybrid and physically read-only replay timing worker.
use pollardai::*;
use std::{
    env,
    hint::black_box,
    path::PathBuf,
    time::{Instant, SystemTime, UNIX_EPOCH},
};
fn payload(i: usize) -> Value {
    json!({"model":"offline","prompt":"Local SQLite benchmark","i":i})
}
fn response() -> Value {
    json!({"text":"ok","usage":{"input_tokens":2,"output_tokens":3}})
}
fn options() -> CallOptions {
    CallOptions {
        estimated_tokens: Some(5),
        ..Default::default()
    }
}
struct Database(PathBuf);
impl Database {
    fn new() -> Self {
        Self(env::temp_dir().join(format!(
                "pollard-sqlite-bench-{}-{}.db",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
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
fn sample(mode: ReplayMode, n: usize) -> (f64, String) {
    let path = Database::new();
    let base = Runtime::new(SQLiteStore::open(&path.0).unwrap(), ReplayMode::Record);
    if mode != ReplayMode::Record {
        let mut setup = base.run("sqlite-benchmark", None, 0).unwrap();
        for i in 0..n {
            setup
                .model_call(payload(i), options(), |_| Ok(response()))
                .unwrap();
        }
    }
    let runtime = if mode == ReplayMode::Record {
        base
    } else {
        drop(base);
        let store = if mode == ReplayMode::Replay {
            SQLiteStore::open_read_only(&path.0).unwrap()
        } else {
            SQLiteStore::open(&path.0).unwrap()
        };
        Runtime::new(store, mode)
    };
    let mut run = runtime.run("sqlite-benchmark", None, 0).unwrap();
    let mut calls = 0;
    let started = Instant::now();
    for i in 0..n {
        black_box(
            run.model_call(payload(i), options(), |_| {
                assert_eq!(mode, ReplayMode::Record);
                calls += 1;
                Ok(response())
            })
            .unwrap(),
        );
    }
    let seconds = started.elapsed().as_secs_f64();
    assert_eq!(calls, if mode == ReplayMode::Record { n } else { 0 });
    let report = run.report().unwrap();
    assert_eq!(report.spent["steps"], n as f64);
    assert_eq!(report.spent["tokens"], (5 * n) as f64);
    let nodes = runtime.store().walk(run.root_id()).unwrap();
    assert_eq!(nodes.len(), n + 1);
    assert!(verify_subtree(&*runtime.store(), run.root_id()).ok);
    let checksum = result_digest_from_text(
        &nodes
            .iter()
            .map(|n| format!("{}:{}", n.id, n.result_digest.as_deref().unwrap_or("")))
            .collect::<Vec<_>>()
            .join("\n"),
    );
    (seconds, checksum)
}
fn main() {
    let args: Vec<String> = env::args().collect();
    let operation = &args[1];
    let n: usize = args[2].parse().unwrap();
    let repeats: usize = args[3].parse().unwrap();
    let mode = match operation.as_str() {
        "record" => ReplayMode::Record,
        "replay" => ReplayMode::Replay,
        "hybrid" => ReplayMode::Hybrid,
        _ => panic!("invalid operation"),
    };
    assert!(n > 0 && repeats > 0);
    for _ in 0..2 {
        sample(mode, n);
    }
    let mut times = Vec::new();
    let mut checksums = Vec::new();
    for _ in 0..repeats {
        let (t, c) = sample(mode, n);
        times.push(t);
        checksums.push(c);
    }
    println!(
        "{}",
        json!({"operation":operation,"size":n,"samples_seconds":times,"checksums":checksums,"warmups":2,"sqlite_version":rusqlite::version()})
    );
}
