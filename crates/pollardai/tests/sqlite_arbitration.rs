use pollardai::*;
use rust_decimal::Decimal;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Barrier};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "pollard-arbitration-{}-{}.db",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        )))
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.0.display()));
        }
    }
}
fn amount(v: i64) -> Decimal {
    Decimal::from(v)
}
fn charges(meter: &str, v: i64) -> BTreeMap<String, Decimal> {
    BTreeMap::from([(meter.into(), amount(v))])
}
fn budget(limit: i64, estimate: i64) -> BudgetReservation {
    BudgetReservation {
        scope_id: "shared-root".into(),
        limits: charges("steps", limit),
        baseline: BTreeMap::new(),
        estimates: charges("steps", estimate),
    }
}
fn window(limit: i64) -> WindowReservation {
    WindowReservation {
        ledger_key: "requests-window".into(),
        meter: "requests".into(),
        limit: amount(limit),
        amount: amount(1),
        window_seconds: 60.0,
    }
}

#[test]
fn decimal_text_preserves_python_ledger_spelling_and_rejects_rounded_settlement() {
    let temp = Temp::new();
    let mut store = SQLiteStore::open(&temp.0).unwrap();
    let map = |v: &str| BTreeMap::from([("usd".into(), v.into())]);
    let b = TextBudgetReservation {
        scope_id: "text".into(),
        limits: map("1E+6"),
        baseline: map("1E+2"),
        estimates: map("1.0E+2"),
    };
    let w = TextWindowReservation {
        ledger_key: "text-window".into(),
        meter: "usd".into(),
        limit: "1E+6".into(),
        amount: "-0.00".into(),
        window_seconds: 30.0,
    };
    assert!(
        store
            .reserve_decimal_text("text", &[b], &[w], 60.0)
            .unwrap()
            .ok
    );
    let connection = rusqlite::Connection::open(&temp.0).unwrap();
    let scalar = |sql: &str| {
        connection
            .query_row(sql, [], |r| r.get::<_, String>(0))
            .unwrap()
    };
    assert_eq!(
        scalar("SELECT settled FROM budget_state WHERE scope_id='text'"),
        "1E+2"
    );
    assert_eq!(
        scalar("SELECT amount FROM reservations WHERE kind='budget'"),
        "1.0E+2"
    );
    assert_eq!(
        scalar("SELECT amount FROM reservations WHERE kind='window'"),
        "-0.00"
    );
    store.settle_decimal_text("text", &map("1E+2")).unwrap();
    assert_eq!(
        scalar("SELECT settled FROM budget_state WHERE scope_id='text'"),
        "2E+2"
    );
    assert_eq!(scalar("SELECT amount FROM window_events"), "1E+2");
    let b = TextBudgetReservation {
        scope_id: "large".into(),
        limits: map(&Decimal::MAX.to_string()),
        baseline: map(&Decimal::MAX.to_string()),
        ..Default::default()
    };
    store
        .reserve_decimal_text("large", &[b], &[], 60.0)
        .unwrap();
    assert!(store.settle_decimal_text("large", &map("1e-28")).is_err());
    assert_eq!(
        scalar("SELECT settled FROM budget_state WHERE scope_id='large'"),
        Decimal::MAX.to_string()
    );
    assert_eq!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM reservations WHERE reservation_id='large'",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
}

#[test]
fn independent_connections_reserve_exact_shared_budget_under_race() {
    let path = Temp::new();
    drop(SQLiteStore::open(path.0.as_path()).unwrap());
    let barrier = Arc::new(Barrier::new(8));
    let workers: Vec<_> = (0..8)
        .map(|id| {
            let path = path.0.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let mut store = SQLiteStore::open(path).unwrap();
                barrier.wait();
                let reservation = format!("worker-{id}");
                let accepted = store
                    .reserve(&reservation, &[budget(3, 1)], &[], 30.0)
                    .unwrap()
                    .ok;
                if accepted {
                    store.settle(&reservation, &charges("steps", 1)).unwrap();
                }
                accepted
            })
        })
        .collect();
    let accepted = workers
        .into_iter()
        .map(|worker| worker.join().unwrap() as usize)
        .sum::<usize>();
    assert_eq!(accepted, 3);
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    let denied = store.reserve("after", &[budget(3, 1)], &[], 30.0).unwrap();
    assert!(!denied.ok);
    assert_eq!(denied.remaining, Decimal::ZERO);
}
#[test]
fn imported_sqlite_ledger_rejects_precision_loss_without_mutating_state() {
    for value in [
        "1e-29",
        "0.00000000000000000000000000001",
        "1.23456789012345678901234567891",
    ] {
        let path = Temp::new();
        let mut store = SQLiteStore::open(&path.0).unwrap();
        assert!(
            store
                .reserve("setup", &[budget(3, 1)], &[], 30.0)
                .unwrap()
                .ok
        );
        store.release("setup").unwrap();
        let connection = rusqlite::Connection::open(&path.0).unwrap();
        connection
            .execute("UPDATE budget_state SET settled=?1", [value])
            .unwrap();
        assert!(
            store
                .reserve("bad-ledger", &[budget(3, 1)], &[], 30.0)
                .is_err(),
            "{value}"
        );
        let stored: String = connection
            .query_row("SELECT settled FROM budget_state", [], |row| row.get(0))
            .unwrap();
        assert_eq!(stored, value);
        let reservations: u64 = connection
            .query_row("SELECT COUNT(*) FROM reservations", [], |row| row.get(0))
            .unwrap();
        assert_eq!(reservations, 0);
    }
}

#[test]
fn windows_include_pending_and_settled_usage_across_connections() {
    let path = Temp::new();
    let mut a = SQLiteStore::open(path.0.as_path()).unwrap();
    let mut b = SQLiteStore::open(path.0.as_path()).unwrap();
    assert!(a.reserve("one", &[], &[window(1)], 30.0).unwrap().ok);
    let refused = b.reserve("two", &[], &[window(1)], 30.0).unwrap();
    assert!(!refused.ok);
    assert_eq!(refused.reason, "window");
    assert_eq!(refused.window_seconds, Some(60.0));
    a.settle("one", &charges("requests", 1)).unwrap();
    assert!(!b.reserve("three", &[], &[window(1)], 30.0).unwrap().ok);
    {
        let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
        c.execute("UPDATE window_events SET settled_at=0", [])
            .unwrap();
    }
    assert!(
        b.reserve("after-window", &[], &[window(1)], 30.0)
            .unwrap()
            .ok
    );
}

#[test]
fn release_returns_capacity_and_repeated_settlement_does_not_double_charge() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    assert!(
        store
            .reserve("released", &[budget(2, 2)], &[], 30.0)
            .unwrap()
            .ok
    );
    store.release("released").unwrap();
    assert!(
        store
            .reserve("actual", &[budget(2, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    store.settle("actual", &charges("steps", 1)).unwrap();
    store.settle("actual", &charges("steps", 1)).unwrap();
    assert!(
        store
            .reserve("last", &[budget(2, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    assert!(
        !store
            .reserve("denied", &[budget(2, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
}

#[test]
fn expired_reservation_stops_blocking_but_late_settlement_keeps_actual_spend() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    assert!(
        store
            .reserve("expired", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    {
        let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
        c.execute("UPDATE reservations SET expires_at=0", [])
            .unwrap();
    }
    assert!(!store.renew("expired", 30.0).unwrap());
    assert!(
        store
            .reserve("replacement", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    store.settle("expired", &charges("steps", 1)).unwrap();
    let denied = store.reserve("later", &[budget(1, 1)], &[], 30.0).unwrap();
    assert_eq!(denied.remaining, amount(-1));
}

#[test]
fn lease_renewal_extends_all_live_rows_and_missing_is_false() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    assert!(!store.renew("missing", 10.0).unwrap());
    assert!(
        store
            .reserve("live", &[budget(10, 1)], &[window(10)], 1.0)
            .unwrap()
            .ok
    );
    assert!(store.renew("live", 1000.0).unwrap());
    let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
    let (count, min, max): (i64, f64, f64) = c
        .query_row(
            "SELECT COUNT(*),MIN(expires_at),MAX(expires_at) FROM reservations",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(count, 2);
    assert_eq!(min, max);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    assert!(min > now + 900.0);
}

#[test]
fn cumulative_baseline_never_decreases_and_decimals_are_exact() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    let request = BudgetReservation {
        scope_id: "cost".into(),
        limits: BTreeMap::from([("usd".into(), Decimal::from_str("0.3").unwrap())]),
        baseline: BTreeMap::from([("usd".into(), Decimal::from_str("0.1").unwrap())]),
        estimates: BTreeMap::from([("usd".into(), Decimal::from_str("0.1").unwrap())]),
    };
    assert!(
        store
            .reserve("one", std::slice::from_ref(&request), &[], 30.0)
            .unwrap()
            .ok
    );
    store.settle("one", &request.estimates).unwrap();
    assert!(
        store
            .reserve("two", std::slice::from_ref(&request), &[], 30.0)
            .unwrap()
            .ok
    );
    store.settle("two", &request.estimates).unwrap();
    let denied = store.reserve("three", &[request], &[], 30.0).unwrap();
    assert!(!denied.ok);
    assert_eq!(denied.remaining, Decimal::ZERO);
}

#[test]
fn scope_failure_does_not_leave_partial_reservations() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    let mut second = budget(0, 1);
    second.scope_id = "z-refused".into();
    assert!(
        !store
            .reserve("partial", &[budget(1, 1), second], &[], 30.0)
            .unwrap()
            .ok
    );
    let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
    let count: u64 = c
        .query_row("SELECT COUNT(*) FROM reservations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 0);
    assert!(
        store
            .reserve("accepted", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
}

#[test]
fn malformed_lease_duplicate_id_and_negative_settlement_fail_without_changes() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    for lease in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert!(store.reserve("bad", &[budget(10, 1)], &[], lease).is_err());
    }
    assert!(
        store
            .reserve("once", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    assert!(store.reserve("once", &[budget(1, 1)], &[], 30.0).is_err());
    assert!(store.settle("once", &charges("steps", -1)).is_err());
    assert!(
        !store
            .reserve("blocked", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    store.release("once").unwrap();
    assert!(
        store
            .reserve("after-release", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
}

#[test]
fn corrupt_settlement_rolls_back_all_rows_and_keeps_reservation() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    let mut request = budget(10, 1);
    request.limits.insert("usd".into(), amount(10));
    request.estimates.insert("usd".into(), amount(1));
    assert!(store.reserve("corrupt", &[request], &[], 30.0).unwrap().ok);
    let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
    c.execute(
        "UPDATE budget_state SET settled='not-a-number' WHERE meter='usd'",
        [],
    )
    .unwrap();
    assert!(store
        .settle(
            "corrupt",
            &BTreeMap::from([("steps".into(), amount(1)), ("usd".into(), amount(1))])
        )
        .is_err());
    let steps: String = c
        .query_row(
            "SELECT settled FROM budget_state WHERE meter='steps'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(steps, "0");
    let reservations: u64 = c
        .query_row("SELECT COUNT(*) FROM reservations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(reservations, 2);
}

#[test]
fn heartbeat_uses_an_independent_thread_safe_connection() {
    let path = Temp::new();
    let mut store = SQLiteStore::open(path.0.as_path()).unwrap();
    assert!(
        store
            .reserve("heartbeat", &[budget(1, 1)], &[], 30.0)
            .unwrap()
            .ok
    );
    let renewer = store.lease_renewer().unwrap();
    assert!(std::thread::spawn(move || renewer("heartbeat", 60.0))
        .join()
        .unwrap()
        .unwrap());
    store.release("heartbeat").unwrap();
    assert!(!store.lease_renewer().unwrap()("heartbeat", 60.0).unwrap());
}

#[test]
fn heartbeat_never_recreates_deleted_database_or_repairs_changed_schema() {
    let path = Temp::new();
    let store = SQLiteStore::open(path.0.as_path()).unwrap();
    let renewer = store.lease_renewer().unwrap();
    drop(store);
    {
        let c = rusqlite::Connection::open(path.0.as_path()).unwrap();
        c.execute("UPDATE kv SET v='999' WHERE k='schema_version'", [])
            .unwrap();
    }
    assert!(renewer("missing", 30.0).is_err());
    std::fs::remove_file(path.0.as_path()).unwrap();
    assert!(renewer("missing", 30.0).is_err());
    assert!(!path.0.exists());
}
