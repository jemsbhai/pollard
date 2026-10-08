//! Exercise Python/Rust shared reservation-table interoperability.
use pollardai::*;
use rust_decimal::Decimal;
use std::collections::BTreeMap;

fn main() -> Result<()> {
    let path = std::env::args()
        .nth(1)
        .ok_or_else(|| Error::Invalid("usage: arbitration_interop STORE.db".into()))?;
    let mut store = SQLiteStore::open(path)?;
    let request = BudgetReservation {
        scope_id: "python-scope".into(),
        limits: BTreeMap::from([("steps".into(), Decimal::ONE)]),
        baseline: BTreeMap::new(),
        estimates: BTreeMap::from([("steps".into(), Decimal::ONE)]),
    };
    let blocked = store.reserve("rust-blocked", &[request], &[], 60.0)?;
    if blocked.ok {
        return Err(Error::Integrity(
            "Rust ignored active Python reservation".into(),
        ));
    }
    store.settle(
        "python-live",
        &BTreeMap::from([("steps".into(), Decimal::ONE)]),
    )?;
    let usd = BudgetReservation {
        scope_id: "rust-scope".into(),
        limits: BTreeMap::from([("usd".into(), Decimal::new(3, 1))]),
        baseline: BTreeMap::new(),
        estimates: BTreeMap::from([("usd".into(), Decimal::new(1, 1))]),
    };
    if !store.reserve("rust-cost", &[usd], &[], 60.0)?.ok {
        return Err(Error::Integrity("unexpected cost refusal".into()));
    }
    store.settle(
        "rust-cost",
        &BTreeMap::from([("usd".into(), Decimal::new(1, 1))]),
    )?;
    let window = WindowReservation {
        ledger_key: "shared-window".into(),
        meter: "requests".into(),
        limit: Decimal::ONE,
        amount: Decimal::ONE,
        window_seconds: 60.0,
    };
    if !store.reserve("rust-window", &[], &[window], 60.0)?.ok {
        return Err(Error::Integrity("unexpected window refusal".into()));
    }
    println!(
        "{}",
        json!({"python_active_blocked_rust":true,"python_reservation_settled_by_rust":true,"rust_settled_usd":"0.1","rust_window_active":true})
    );
    Ok(())
}
