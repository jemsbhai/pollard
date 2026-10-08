//! SQLite-backed runtime cleanup and cancellation contract, independent of arbiter unit tests.
use futures::{future::pending, task::noop_waker};
use pollardai::*;
use rust_decimal::Decimal;
use std::future::Future;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::{Context, Poll};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Database(PathBuf);
impl Database {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "pollard-runtime-contract-{}-{}.db",
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
fn prepared(label: &str) -> (Database, Runtime, Run) {
    let db = Database::new();
    let runtime = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Record)
        .with_meter(WindowMeter::new_decimal("requests", "1", 60.0, None).unwrap())
        .with_reservation_lease_seconds(1.0)
        .unwrap();
    let run = runtime
        .run(
            label,
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    (db, runtime, run)
}
fn reservations(db: &Database) -> u64 {
    rusqlite::Connection::open(&db.0)
        .unwrap()
        .query_row("SELECT COUNT(*) FROM reservations", [], |r| r.get(0))
        .unwrap()
}
fn settled(db: &Database, meter: &str) -> Decimal {
    let connection = rusqlite::Connection::open(&db.0).unwrap();
    let rows: Vec<String> = connection
        .prepare("SELECT settled FROM budget_state WHERE meter=?1")
        .unwrap()
        .query_map([meter], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap();
    rows.iter().map(|v| Decimal::from_str(v).unwrap()).sum()
}
fn window_spend(db: &Database) -> Decimal {
    let connection = rusqlite::Connection::open(&db.0).unwrap();
    let rows: Vec<String> = connection
        .prepare("SELECT amount FROM window_events")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap();
    rows.iter().map(|v| Decimal::from_str(v).unwrap()).sum()
}
fn response() -> Value {
    json!({"text":"ok","usage":{"input_tokens":0,"output_tokens":0}})
}

#[test]
fn ordinary_callback_error_cleans_reservations_and_preserves_capacity() {
    let (db, _runtime, mut run) = prepared("known-error");
    let root = run.root_id().to_owned();
    let error = run
        .model_call(json!({"known-error":true}), CallOptions::default(), |_| {
            assert_eq!(reservations(&db), 2);
            Err(Error::Handler("known failure".into()))
        })
        .unwrap_err();
    assert!(matches!(error, Error::Handler(_)));
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ZERO);
    assert_eq!(window_spend(&db), Decimal::ZERO);
    assert_eq!(run.cursor_id(), root);
    run.model_call(json!({"known-error":true}), CallOptions::default(), |_| {
        Ok(response())
    })
    .unwrap();
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ONE);
    assert_eq!(window_spend(&db), Decimal::ONE);
}

#[test]
fn unknown_callback_error_settles_estimates_and_prevents_unaccounted_retry() {
    let (db, runtime, mut run) = prepared("unknown-error");
    let error = run
        .model_call(json!({"unknown":true}), CallOptions::default(), |_| {
            Err(Error::OutcomeUnknown(Box::new(Error::Handler(
                "network lost".into(),
            ))))
        })
        .unwrap_err();
    assert!(error.is_post_dispatch_outcome_unknown());
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ONE);
    assert_eq!(window_spend(&db), Decimal::ONE);
    let failed = run.cursor().unwrap();
    assert_eq!(failed.meta["state"], "failed");
    assert_eq!(failed.meta["accounting_unknown"], true);
    let mut next = runtime
        .run(
            "unknown-error",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        next.model_call(json!({"new":true}), CallOptions::default(), |_| panic!(
            "must refuse"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}

#[test]
fn async_cancellation_settles_once_and_releases_sqlite_lease_and_runtime_guard() {
    let (db, runtime, mut run) = prepared("async-cancel");
    let root = run.root_id().to_owned();
    let mut call = Box::pin(run.amodel_call(
        json!({"cancel":true}),
        CallOptions::default(),
        |_| pending::<Result<Value>>(),
    ));
    let waker = noop_waker();
    assert!(matches!(
        call.as_mut().poll(&mut Context::from_waker(&waker)),
        Poll::Pending
    ));
    assert_eq!(reservations(&db), 2);
    assert!(matches!(
        runtime.run("guard-held", None, 0),
        Err(Error::Busy)
    ));
    drop(call);
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ONE);
    assert_eq!(window_spend(&db), Decimal::ONE);
    assert!(runtime.run("guard-released", None, 0).is_ok());
    let nodes = runtime.store().walk(&root).unwrap();
    assert_eq!(nodes.len(), 2);
    assert_eq!(nodes[1].meta["state"], "failed");
    assert_eq!(nodes[1].meta["error"], "outcome_unknown");
    drop(run);
    drop(runtime);
    let reopened = Runtime::new(SQLiteStore::open(&db.0).unwrap(), ReplayMode::Record);
    let mut retry = reopened
        .run(
            "async-cancel",
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    assert!(matches!(
        retry.model_call(json!({"new":true}), CallOptions::default(), |_| panic!(
            "must refuse"
        )),
        Err(Error::BudgetExceeded { .. })
    ));
}

#[test]
fn panicking_callback_conservatively_settles_and_does_not_leak_reservation() {
    let (db, _runtime, mut run) = prepared("panic");
    let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run.model_call(json!({}), CallOptions::default(), |_| {
            panic!("provider panic")
        })
    }));
    assert!(panic.is_err());
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ONE);
    assert_eq!(window_spend(&db), Decimal::ONE);
    assert_eq!(run.cursor().unwrap().meta["state"], "failed");
}

#[test]
fn heartbeat_renews_during_slow_failure_then_releases_every_row() {
    let (db, _runtime, mut run) = prepared("slow-failure");
    let outcome = run.model_call(json!({}), CallOptions::default(), |_| {
        let connection = rusqlite::Connection::open(&db.0).unwrap();
        let original: f64 = connection
            .query_row("SELECT MIN(expires_at) FROM reservations", [], |r| r.get(0))
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
        loop {
            let renewed: f64 = connection
                .query_row("SELECT MIN(expires_at) FROM reservations", [], |r| r.get(0))
                .unwrap();
            if renewed > original + 0.1 {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "heartbeat never extended its lease"
            );
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        Err(Error::Handler("known failure after long work".into()))
    });
    assert!(matches!(outcome, Err(Error::Handler(_))));
    assert_eq!(reservations(&db), 0);
    assert_eq!(settled(&db, "steps"), Decimal::ZERO);
    assert_eq!(window_spend(&db), Decimal::ZERO);
}
