//! SQLite schema-v3 storage interoperable with Python Pollard 1.6.0.
//!
//! Writes, pending settlement, cumulative budget reservations, and sliding windows
//! use SQLite transactions and Python-compatible ledger tables.
use crate::identity::{hash, hex64};
use crate::{canonical_bytes, Error, Node, NodeKind, RecordingStore, Result, Store};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// One cumulative budget scope for explicit shared SQLite arbitration.
#[derive(Debug, Clone, Default)]
pub struct BudgetReservation {
    pub scope_id: String,
    pub limits: BTreeMap<String, Decimal>,
    pub baseline: BTreeMap<String, Decimal>,
    pub estimates: BTreeMap<String, Decimal>,
}
#[derive(Debug, Clone)]
pub struct WindowReservation {
    pub ledger_key: String,
    pub meter: String,
    pub limit: Decimal,
    pub amount: Decimal,
    pub window_seconds: f64,
}
#[derive(Debug, Clone, PartialEq)]
pub struct ReservationCheck {
    pub ok: bool,
    pub reason: String,
    pub meter: Option<String>,
    pub requested: Decimal,
    pub remaining: Decimal,
    pub window_seconds: Option<f64>,
}
impl Default for ReservationCheck {
    fn default() -> Self {
        Self {
            ok: true,
            reason: "budget".into(),
            meter: None,
            requested: Decimal::ZERO,
            remaining: Decimal::ZERO,
            window_seconds: None,
        }
    }
}

const SCHEMA: &str = "
CREATE TABLE nodes(id TEXT PRIMARY KEY,parent TEXT,kind TEXT NOT NULL,attempt INTEGER NOT NULL,payload TEXT NOT NULL,result TEXT,result_digest TEXT,meta TEXT NOT NULL);
CREATE INDEX idx_nodes_parent ON nodes(parent);
CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
INSERT INTO kv VALUES('schema_version','3');
CREATE TABLE blobs(digest TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE blob_literals(node_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(node_id,path));
CREATE TABLE budget_state(scope_id TEXT NOT NULL,meter TEXT NOT NULL,settled TEXT NOT NULL,PRIMARY KEY(scope_id,meter));
CREATE TABLE reservations(reservation_id TEXT NOT NULL,kind TEXT NOT NULL,scope_id TEXT NOT NULL,meter TEXT NOT NULL,amount TEXT NOT NULL,expires_at REAL NOT NULL,window_seconds REAL,PRIMARY KEY(reservation_id,kind,scope_id,meter));
CREATE INDEX idx_reservations_scope ON reservations(kind,scope_id,meter,expires_at);
CREATE TABLE window_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,scope_id TEXT NOT NULL,meter TEXT NOT NULL,amount TEXT NOT NULL,settled_at REAL NOT NULL);
CREATE INDEX idx_window_events_scope ON window_events(scope_id,meter,settled_at);";

fn db_error(e: rusqlite::Error) -> Error {
    Error::Integrity(format!("SQLite: {e}"))
}
fn encode(v: &Value) -> Result<String> {
    serde_json::to_string(v).map_err(|e| Error::Invalid(e.to_string()))
}

pub struct SQLiteStore {
    connection: Connection,
    read_only: bool,
    intern_threshold: Option<usize>,
    in_memory: bool,
    path: PathBuf,
    local_revision: Cell<u64>,
}

impl SQLiteStore {
    /// Explicitly migrate an existing schema 0/1/2 database in one transaction.
    /// Ordinary opens and all read-only access remain non-migrating. Back up an
    /// existing recording before requesting migration. Unknown versions and
    /// malformed nodes are rejected without committing schema/data changes.
    pub fn migrate_legacy(path: impl AsRef<Path>) -> Result<Self> {
        let connection =
            Connection::open_with_flags(path.as_ref(), OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(db_error)?;
        connection
            .busy_timeout(Duration::from_secs(30))
            .map_err(db_error)?;
        let path =
            std::fs::canonicalize(path.as_ref()).map_err(|e| Error::Invalid(e.to_string()))?;
        let mut store = Self {
            connection,
            path,
            read_only: false,
            intern_threshold: Some(1024),
            in_memory: false,
            local_revision: Cell::new(0),
        };
        store.transaction(|store| {
            store.connection.prepare("SELECT id,parent,kind,attempt,payload,result,result_digest,meta FROM nodes LIMIT 0").map_err(db_error)?;
            let version:Option<String>=store.connection.query_row("SELECT v FROM kv WHERE k='schema_version'",[],|r|r.get(0)).optional().map_err(db_error)?;
            let version=version.as_deref().unwrap_or("0").parse::<u32>().map_err(|_|Error::Integrity("invalid SQLite schema version".into()))?;
            if version>3{return Err(Error::Integrity("unsupported SQLite schema version".into()));}
            if version==3{return Err(Error::Invalid("database is already schema 3; open it normally".into()));}
            let schema=SCHEMA.lines().filter(|line|!line.starts_with("INSERT INTO kv"))
                .collect::<Vec<_>>().join("\n").replace("CREATE TABLE ","CREATE TABLE IF NOT EXISTS ").replace("CREATE INDEX ","CREATE INDEX IF NOT EXISTS ");
            store.connection.execute_batch(&schema).map_err(db_error)?;
            if version<2 {
                let rows:Vec<(String,String)>=store.connection.prepare("SELECT id,payload FROM nodes").map_err(db_error)?.query_map([],|r|Ok((r.get(0)?,r.get(1)?))).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
                for(id,payload)in rows {
                    let payload:Value=serde_json::from_str(&payload).map_err(|e|Error::Integrity(e.to_string()))?;
                    let mut ignored=BTreeMap::new();let mut literals=Vec::new();
                    intern(payload,&mut Vec::new(),None,&mut ignored,&mut literals)?;
                    for path in literals {store.connection.execute("INSERT OR IGNORE INTO blob_literals(node_id,path) VALUES(?1,?2)",params![id,path]).map_err(db_error)?;}
                }
            }
            let ids:Vec<String>=store.connection.prepare("SELECT id FROM nodes").map_err(db_error)?.query_map([],|r|r.get(0)).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
            for id in ids {
                let node=store.get(&id)?;node.validate()?;
                if let Some(parent)=node.parent {if !store.try_exists(&parent)? {return Err(Error::Integrity("legacy SQLite node has a missing parent".into()));}}
            }
            store.connection.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('schema_version','3')",[]).map_err(db_error)?;
            Ok(())
        })?;
        store
            .connection
            .execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            .map_err(db_error)?;
        Ok(store)
    }
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        Self::open_with_options(path, false, Some(1024))
    }
    pub fn open_read_only(path: impl AsRef<Path>) -> Result<Self> {
        Self::open_with_options(path, true, Some(1024))
    }
    /// None disables interning. Existing legacy schemas are refused, never repaired.
    pub fn open_with_options(
        path: impl AsRef<Path>,
        read_only: bool,
        intern_threshold: Option<usize>,
    ) -> Result<Self> {
        if intern_threshold == Some(0) {
            return Err(Error::Invalid("intern threshold must be positive".into()));
        }
        let flags = if read_only {
            OpenFlags::SQLITE_OPEN_READ_ONLY
        } else {
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
        };
        let in_memory = path.as_ref() == Path::new(":memory:");
        let connection = Connection::open_with_flags(path.as_ref(), flags).map_err(db_error)?;
        let path = if in_memory {
            PathBuf::from(":memory:")
        } else {
            std::fs::canonicalize(path.as_ref()).map_err(|e| Error::Invalid(e.to_string()))?
        };
        connection
            .busy_timeout(Duration::from_secs(30))
            .map_err(db_error)?;
        let tables: BTreeSet<String> = connection
            .prepare("SELECT name FROM sqlite_master WHERE type='table'")
            .map_err(db_error)?
            .query_map([], |r| r.get(0))
            .map_err(db_error)?
            .collect::<std::result::Result<_, _>>()
            .map_err(db_error)?;
        if tables.is_empty() && !read_only {
            connection
                .execute_batch(&format!("BEGIN IMMEDIATE;{SCHEMA}COMMIT;"))
                .map_err(db_error)?;
        } else {
            for name in ["nodes", "kv", "blobs", "blob_literals"] {
                if !tables.contains(name) {
                    return Err(Error::Integrity(format!(
                        "SQLite recording missing table {name}"
                    )));
                }
            }
            if !read_only {
                for name in ["budget_state", "reservations", "window_events"] {
                    if !tables.contains(name) {
                        return Err(Error::Integrity(format!(
                            "writable SQLite recording missing table {name}"
                        )));
                    }
                }
            }
            let version: Option<String> = connection
                .query_row("SELECT v FROM kv WHERE k='schema_version'", [], |r| {
                    r.get(0)
                })
                .optional()
                .map_err(db_error)?;
            if version.as_deref() != Some("3") {
                return Err(Error::Integrity(format!("SQLite requires schema version 3; found {version:?}; explicitly migrate a backed-up copy with SQLiteStore::migrate_legacy")));
            }
            // Prepare all identity columns before changing any database settings.
            connection.prepare("SELECT id,parent,kind,attempt,payload,result,result_digest,meta FROM nodes LIMIT 0").map_err(db_error)?;
        }
        if read_only {
            connection
                .execute_batch("PRAGMA query_only=ON;")
                .map_err(db_error)?;
        } else {
            connection
                .execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
                .map_err(db_error)?;
        }
        Ok(Self {
            connection,
            read_only,
            intern_threshold,
            in_memory,
            path,
            local_revision: Cell::new(0),
        })
    }
    pub fn is_read_only(&self) -> bool {
        self.read_only
    }

    /// Explicit reserve operation, serialized across independent SQLite connections.
    /// Direct callers must settle or release accepted reservations and renew leases
    /// while work is in flight. RecordingStore hooks expose these operations to Runtime.
    pub fn reserve(
        &mut self,
        reservation_id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease_seconds: f64,
    ) -> Result<ReservationCheck> {
        self.reserve_with_spelling(reservation_id, budgets, windows, lease_seconds, None)
    }
    /// Preserve Python Decimal text in SQLite ledger rows. SQLite's schema-v3
    /// retry semantics are unchanged (active duplicate IDs are refused).
    pub fn reserve_decimal_text(
        &mut self,
        reservation_id: &str,
        budgets: &[crate::TextBudgetReservation],
        windows: &[crate::TextWindowReservation],
        lease_seconds: f64,
    ) -> Result<ReservationCheck> {
        let prepared = crate::decimal_wire::prepare_request(budgets, windows, lease_seconds)?;
        self.reserve_with_spelling(
            reservation_id,
            &prepared.budgets,
            &prepared.windows,
            lease_seconds,
            Some(&prepared.document),
        )
    }
    fn reserve_with_spelling(
        &mut self,
        reservation_id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease_seconds: f64,
        spelling: Option<&Value>,
    ) -> Result<ReservationCheck> {
        validate_lease(lease_seconds)?;
        if reservation_id.is_empty() {
            return Err(Error::Invalid("reservation_id must be nonempty".into()));
        }
        let mut keys = BTreeSet::new();
        for budget in budgets {
            if budget.scope_id.is_empty() {
                return Err(Error::Invalid("budget scope must be nonempty".into()));
            }
            for (meter, limit) in &budget.limits {
                if meter.is_empty()
                    || *limit < Decimal::ZERO
                    || !keys.insert(("budget", budget.scope_id.as_str(), meter.as_str()))
                {
                    return Err(Error::Invalid(
                        "invalid or duplicate budget reservation".into(),
                    ));
                }
            }
            if budget
                .baseline
                .values()
                .chain(budget.estimates.values())
                .any(|v| *v < Decimal::ZERO)
            {
                return Err(Error::Invalid(
                    "reservation amounts must be nonnegative".into(),
                ));
            }
        }
        for window in windows {
            if window.ledger_key.is_empty()
                || window.meter.is_empty()
                || window.limit <= Decimal::ZERO
                || window.amount < Decimal::ZERO
                || !window.window_seconds.is_finite()
                || window.window_seconds <= 0.0
                || !keys.insert(("window", window.ledger_key.as_str(), window.meter.as_str()))
            {
                return Err(Error::Invalid(
                    "invalid or duplicate window reservation".into(),
                ));
            }
        }
        self.transaction(|s|{
            let now=epoch()?;
            let active=s.connection.query_row("SELECT 1 FROM reservations WHERE reservation_id=?1 LIMIT 1",[reservation_id],|_|Ok(())).optional().map_err(db_error)?;
            if active.is_some(){return Err(Error::Invalid("reservation id already exists".into()));}
            let mut ordered:Vec<_>=budgets.iter().collect();ordered.sort_by_key(|b|&b.scope_id);
            for request in ordered {
                for (meter,limit) in &request.limits {
                    if meter=="depth"{continue;}
                    let baseline=request.baseline.get(meter).copied().unwrap_or_default();
                    let baseline_text=spelling.and_then(|v|crate::decimal_wire::budget_spelling(v,&request.scope_id,meter,"baseline")).map(str::to_owned).unwrap_or_else(||crate::kv::decimal_string(baseline));
                    s.connection.execute("INSERT OR IGNORE INTO budget_state(scope_id,meter,settled) VALUES(?1,?2,?3)",params![request.scope_id,meter,baseline_text]).map_err(db_error)?;
                    let state:String=s.connection.query_row("SELECT settled FROM budget_state WHERE scope_id=?1 AND meter=?2",params![request.scope_id,meter],|r|r.get(0)).map_err(db_error)?;
                    let previous=parse_decimal(&state)?;
                    let settled=if baseline>previous {s.connection.execute("UPDATE budget_state SET settled=?1 WHERE scope_id=?2 AND meter=?3",params![baseline_text,request.scope_id,meter]).map_err(db_error)?;baseline}else{previous};
                    let amounts:Vec<String>=s.connection.prepare("SELECT amount FROM reservations WHERE kind='budget' AND scope_id=?1 AND meter=?2 AND expires_at>?3").map_err(db_error)?.query_map(params![request.scope_id,meter,now],|r|r.get(0)).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
                    let active=sum_decimals(amounts)?;
                    let remaining=crate::decimal::exact_subtract(*limit,settled).and_then(|n|crate::decimal::exact_subtract(n,active)).ok_or_else(||Error::Integrity("budget arithmetic overflow".into()))?;
                    let amount=request.estimates.get(meter).copied().unwrap_or_default();
                    if amount>remaining{return Ok(ReservationCheck {ok:false,meter:Some(meter.clone()),requested:amount,remaining,..Default::default()});}
                }
            }
            let mut ordered:Vec<_>=windows.iter().collect();ordered.sort_by_key(|w|&w.ledger_key);
            for window in ordered {
                let cutoff=now-window.window_seconds;
                s.connection.execute("DELETE FROM window_events WHERE scope_id=?1 AND settled_at<=?2",params![window.ledger_key,cutoff]).map_err(db_error)?;
                let amounts:Vec<String>=s.connection.prepare("SELECT amount FROM window_events WHERE scope_id=?1 AND settled_at>?2").map_err(db_error)?.query_map(params![window.ledger_key,cutoff],|r|r.get(0)).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
                let settled=sum_decimals(amounts)?;
                let amounts:Vec<String>=s.connection.prepare("SELECT amount FROM reservations WHERE kind='window' AND scope_id=?1 AND expires_at>?2").map_err(db_error)?.query_map(params![window.ledger_key,now],|r|r.get(0)).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
                let active=sum_decimals(amounts)?;
                let remaining=crate::decimal::exact_subtract(window.limit,settled).and_then(|n|crate::decimal::exact_subtract(n,active)).ok_or_else(||Error::Integrity("window arithmetic overflow".into()))?;
                if window.amount>remaining{return Ok(ReservationCheck {ok:false,reason:"window".into(),meter:Some(window.meter.clone()),requested:window.amount,remaining,window_seconds:Some(window.window_seconds)});}
            }
            let expires_at=if s.in_memory{f64::INFINITY}else{now+lease_seconds};
            for request in budgets {for meter in request.limits.keys(){if meter=="depth"{continue;}let amount=spelling.and_then(|v|crate::decimal_wire::budget_spelling(v,&request.scope_id,meter,"estimates")).map(str::to_owned).unwrap_or_else(||crate::kv::decimal_string(request.estimates.get(meter).copied().unwrap_or_default()));s.connection.execute("INSERT INTO reservations(reservation_id,kind,scope_id,meter,amount,expires_at,window_seconds) VALUES(?1,'budget',?2,?3,?4,?5,NULL)",params![reservation_id,request.scope_id,meter,amount,expires_at]).map_err(db_error)?;}}
            for window in windows {let amount=spelling.and_then(|v|crate::decimal_wire::window_spelling(v,&window.ledger_key,"amount")).map(str::to_owned).unwrap_or_else(||crate::kv::decimal_string(window.amount));s.connection.execute("INSERT INTO reservations(reservation_id,kind,scope_id,meter,amount,expires_at,window_seconds) VALUES(?1,'window',?2,?3,?4,?5,?6)",params![reservation_id,window.ledger_key,window.meter,amount,expires_at,window.window_seconds]).map_err(db_error)?;}
            Ok(ReservationCheck::default())
        })
    }

    /// Settle each reserved budget/window once, then remove its reservation rows.
    /// Repeating settlement after success is idempotent, matching Python.
    pub fn settle(
        &mut self,
        reservation_id: &str,
        charges: &BTreeMap<String, Decimal>,
    ) -> Result<()> {
        self.settle_with_spelling(reservation_id, charges, None)
    }
    /// Settle using the original Python Decimal scale/exponent for ledger text.
    pub fn settle_decimal_text(
        &mut self,
        reservation_id: &str,
        charges: &BTreeMap<String, String>,
    ) -> Result<()> {
        let prepared = crate::decimal_wire::prepare_charges(charges)?;
        self.settle_with_spelling(reservation_id, &prepared.amounts, Some(&prepared.document))
    }
    fn settle_with_spelling(
        &mut self,
        reservation_id: &str,
        charges: &BTreeMap<String, Decimal>,
        spelling: Option<&Value>,
    ) -> Result<()> {
        if charges.values().any(|v| *v < Decimal::ZERO) {
            return Err(Error::Invalid("settled charges must be nonnegative".into()));
        }
        self.transaction(|s|{
            let now=epoch()?;
            let rows:Vec<(String,String,String)>=s.connection.prepare("SELECT kind,scope_id,meter FROM reservations WHERE reservation_id=?1").map_err(db_error)?.query_map([reservation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).map_err(db_error)?.collect::<std::result::Result<_,_>>().map_err(db_error)?;
            for(kind,scope,meter)in rows {
                let actual=charges.get(&meter).copied().unwrap_or_default();
                let actual_text=spelling.and_then(|v|v.get(&meter)).and_then(Value::as_str).map(str::to_owned).unwrap_or_else(||crate::kv::decimal_string(actual));
                match kind.as_str(){
                    "budget"=>{
                        let state:Option<String>=s.connection.query_row("SELECT settled FROM budget_state WHERE scope_id=?1 AND meter=?2",params![scope,meter],|r|r.get(0)).optional().map_err(db_error)?;
                        let state=state.ok_or_else(||Error::Integrity("budget state missing during settlement".into()))?;
                        parse_decimal(&state)?;
                        let settled=crate::decimal_wire::add_text(&state,&actual_text)?;
                        s.connection.execute("UPDATE budget_state SET settled=?1 WHERE scope_id=?2 AND meter=?3",params![settled,scope,meter]).map_err(db_error)?;
                    }
                    "window"=>{if actual!=Decimal::ZERO{s.connection.execute("INSERT INTO window_events(scope_id,meter,amount,settled_at) VALUES(?1,?2,?3,?4)",params![scope,meter,actual_text,now]).map_err(db_error)?;}}
                    _=>return Err(Error::Integrity("unknown reservation kind".into())),
                }
            }
            s.connection.execute("DELETE FROM reservations WHERE reservation_id=?1",[reservation_id]).map_err(db_error)?;Ok(())
        })
    }
    pub fn release(&mut self, reservation_id: &str) -> Result<()> {
        self.transaction(|s| {
            s.connection
                .execute(
                    "DELETE FROM reservations WHERE reservation_id=?1",
                    [reservation_id],
                )
                .map_err(db_error)?;
            Ok(())
        })
    }
    pub fn renew(&mut self, reservation_id: &str, lease_seconds: f64) -> Result<bool> {
        validate_lease(lease_seconds)?;
        self.transaction(|s| {
            let now = epoch()?;
            let (count, earliest): (u64, Option<f64>) = s
                .connection
                .query_row(
                    "SELECT COUNT(*),MIN(expires_at) FROM reservations WHERE reservation_id=?1",
                    [reservation_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(db_error)?;
            if count == 0 || earliest.map_or(true, |expiry| expiry <= now) {
                return Ok(false);
            }
            let expiry = if s.in_memory {
                f64::INFINITY
            } else {
                now + lease_seconds
            };
            s.connection
                .execute(
                    "UPDATE reservations SET expires_at=?1 WHERE reservation_id=?2",
                    params![expiry, reservation_id],
                )
                .map_err(db_error)?;
            Ok(true)
        })
    }
    fn writable(&self) -> Result<()> {
        if self.read_only {
            Err(Error::Invalid("SQLite store is read-only".into()))
        } else {
            Ok(())
        }
    }
    fn transaction<T>(&mut self, f: impl FnOnce(&Self) -> Result<T>) -> Result<T> {
        self.writable()?;
        self.connection
            .execute_batch("BEGIN IMMEDIATE")
            .map_err(db_error)?;
        // Failed writes and ambiguous COMMIT errors also invalidate prior reads.
        // Increment before work starts so no error path can preserve stale data.
        self.local_revision
            .set(self.local_revision.get().wrapping_add(1));
        match f(self) {
            Ok(value) => match self.connection.execute_batch("COMMIT") {
                Ok(()) => Ok(value),
                Err(error) => {
                    let _ = self.connection.execute_batch("ROLLBACK");
                    Err(db_error(error))
                }
            },
            Err(error) => {
                let _ = self.connection.execute_batch("ROLLBACK");
                Err(error)
            }
        }
    }
    fn get_optional(&self, id: &str) -> Result<Option<Node>> {
        type Row = (
            String,
            Option<String>,
            String,
            u64,
            String,
            Option<String>,
            Option<String>,
            String,
        );
        let row: Option<Row> = self.connection.prepare_cached(
            "SELECT id,parent,kind,attempt,payload,result,result_digest,meta FROM nodes WHERE id=?1").map_err(db_error)?.query_row([id],
            |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))
        ).optional().map_err(db_error)?;
        let Some((id, parent, kind, attempt, payload, result, digest, meta)) = row else {
            return Ok(None);
        };
        let literals: BTreeSet<String> = self
            .connection
            .prepare_cached("SELECT path FROM blob_literals WHERE node_id=?1")
            .map_err(db_error)?
            .query_map([&id], |r| r.get(0))
            .map_err(db_error)?
            .collect::<std::result::Result<_, _>>()
            .map_err(db_error)?;
        let payload: Value =
            serde_json::from_str(&payload).map_err(|e| Error::Integrity(e.to_string()))?;
        let payload = self.rehydrate(payload, &mut Vec::new(), &literals)?;
        let kind: NodeKind =
            serde_json::from_value(json!(kind)).map_err(|e| Error::Integrity(e.to_string()))?;
        Node::from_storage(
            id,
            parent,
            kind,
            attempt,
            &encode(&payload)?,
            result,
            digest,
            &meta,
        )
        .map(Some)
    }
    fn rehydrate(
        &self,
        value: Value,
        path: &mut Vec<Value>,
        literals: &BTreeSet<String>,
    ) -> Result<Value> {
        if let Some(digest) = blob_digest(&value) {
            if !literals.contains(&encode(&json!(path))?) {
                let blob: Option<String> = self
                    .connection
                    .prepare_cached("SELECT value FROM blobs WHERE digest=?1")
                    .map_err(db_error)?
                    .query_row([digest], |r| r.get(0))
                    .optional()
                    .map_err(db_error)?;
                let blob = blob.ok_or_else(|| {
                    Error::Integrity(format!("missing interned payload blob: {digest}"))
                })?;
                if hash(b"", blob.as_bytes()) != digest {
                    return Err(Error::Integrity(
                        "interned payload blob digest mismatch".into(),
                    ));
                }
                return Ok(json!(blob));
            }
            return Ok(value);
        }
        match value {
            Value::Object(values) => {
                let mut out = serde_json::Map::new();
                for (key, value) in values {
                    path.push(json!(key));
                    let value = self.rehydrate(value, path, literals)?;
                    path.pop();
                    out.insert(key, value);
                }
                Ok(Value::Object(out))
            }
            Value::Array(values) => {
                let mut out = Vec::new();
                for (index, value) in values.into_iter().enumerate() {
                    path.push(json!(index));
                    out.push(self.rehydrate(value, path, literals)?);
                    path.pop();
                }
                Ok(Value::Array(out))
            }
            value => Ok(value),
        }
    }
    fn put_locked(&self, node: Node) -> Result<()> {
        node.validate()?;
        if let Some(parent) = &node.parent {
            if !self.exists(parent) {
                return Err(Error::NotFound(parent.clone()));
            }
        }
        if let Some(mut old) = self.get_optional(&node.id)? {
            if !same_identity(&old, &node) {
                return Err(Error::Integrity("node identity collision".into()));
            }
            if node.result_text.is_some() && old.result_text != node.result_text {
                let conflicts = old
                    .meta
                    .as_object_mut()
                    .expect("validated meta")
                    .entry("result_conflicts")
                    .or_insert_with(|| json!([]));
                conflicts
                    .as_array_mut()
                    .ok_or_else(|| Error::Integrity("invalid result_conflicts metadata".into()))?
                    .push(json!({"result_digest":node.result_digest,"result":node.result}));
                self.patch_locked(&old.id, old.meta)?;
            }
            return Ok(());
        }
        let mut blobs = BTreeMap::new();
        let mut literals = Vec::new();
        let payload = intern(
            node.payload.clone(),
            &mut Vec::new(),
            self.intern_threshold,
            &mut blobs,
            &mut literals,
        )?;
        for (digest, value) in blobs {
            let existing: Option<String> = self
                .connection
                .query_row("SELECT value FROM blobs WHERE digest=?1", [&digest], |r| {
                    r.get(0)
                })
                .optional()
                .map_err(db_error)?;
            if existing.as_ref().is_some_and(|old| old != &value) {
                return Err(Error::Integrity("blob digest collision".into()));
            }
            self.connection
                .execute(
                    "INSERT OR IGNORE INTO blobs(digest,value) VALUES(?1,?2)",
                    params![digest, value],
                )
                .map_err(db_error)?;
        }
        let payload = String::from_utf8(canonical_bytes(&payload)?)
            .map_err(|e| Error::Invalid(e.to_string()))?;
        self.connection.execute("INSERT INTO nodes(id,parent,kind,attempt,payload,result,result_digest,meta) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            params![node.id,node.parent,node.kind.as_str(),node.attempt,payload,node.result_text,node.result_digest,encode(&node.meta)?]).map_err(db_error)?;
        for path in literals {
            self.connection
                .execute(
                    "INSERT INTO blob_literals(node_id,path) VALUES(?1,?2)",
                    params![node.id, path],
                )
                .map_err(db_error)?;
        }
        Ok(())
    }
    fn patch_locked(&self, id: &str, patch: Value) -> Result<()> {
        let patch = patch
            .as_object()
            .ok_or_else(|| Error::Invalid("meta patch must be object".into()))?;
        let mut node = self.get(id)?;
        node.meta
            .as_object_mut()
            .expect("validated meta")
            .extend(patch.clone());
        self.connection
            .execute(
                "UPDATE nodes SET meta=?1 WHERE id=?2",
                params![encode(&node.meta)?, id],
            )
            .map_err(db_error)?;
        Ok(())
    }
}

pub(crate) fn same_identity(a: &Node, b: &Node) -> bool {
    a.id == b.id
        && a.parent == b.parent
        && a.kind == b.kind
        && a.attempt == b.attempt
        && a.payload == b.payload
}

impl Store for SQLiteStore {
    fn import_nodes(&mut self, nodes: Vec<Node>) -> Result<(usize, usize)> {
        self.transaction(|s| crate::merge::import_prepared(&mut LockedSQLite(s), nodes))
    }
    fn merge_nodes(&mut self, nodes: Vec<Node>, replay: bool) -> Result<crate::MergeReport> {
        self.transaction(|s| crate::merge::merge_prepared(&mut LockedSQLite(s), nodes, replay))
    }
    fn put(&mut self, node: Node) -> Result<()> {
        self.transaction(|s| s.put_locked(node))
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.get_optional(id)?
            .ok_or_else(|| Error::NotFound(id.into()))
    }
    fn exists(&self, id: &str) -> bool {
        self.connection
            .query_row("SELECT 1 FROM nodes WHERE id=?1", [id], |_| Ok(()))
            .optional()
            .is_ok_and(|v| v.is_some())
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        self.connection
            .prepare_cached("SELECT 1 FROM nodes WHERE id=?1")
            .map_err(db_error)?
            .query_row([id], |_| Ok(()))
            .optional()
            .map(|v| v.is_some())
            .map_err(db_error)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.connection
            .prepare_cached("SELECT id FROM nodes WHERE parent=?1 ORDER BY kind,id")
            .map_err(db_error)?
            .query_map([id], |r| r.get(0))
            .map_err(db_error)?
            .collect::<std::result::Result<_, _>>()
            .map_err(db_error)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.transaction(|s| s.patch_locked(id, patch))
    }
    fn roots(&self) -> Result<Vec<String>> {
        let ids: Vec<String> = self
            .connection
            .prepare("SELECT id FROM nodes WHERE parent IS NULL")
            .map_err(db_error)?
            .query_map([], |r| r.get(0))
            .map_err(db_error)?
            .collect::<std::result::Result<_, _>>()
            .map_err(db_error)?;
        let mut roots = Vec::new();
        for id in ids {
            let node = self.get(&id)?;
            roots.push((
                node.payload
                    .get("run")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                id,
            ));
        }
        roots.sort();
        Ok(roots.into_iter().map(|(_, id)| id).collect())
    }
    fn apply_batch(&mut self, nodes: Vec<Node>, patches: Vec<(String, Value)>) -> Result<()> {
        self.transaction(|s| {
            for node in nodes {
                s.put_locked(node)?;
            }
            for (id, patch) in patches {
                s.patch_locked(&id, patch)?;
            }
            Ok(())
        })
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.transaction(|s| {
            for id in ids {
                if s.children(id)?.iter().any(|child| !ids.contains(child)) {
                    return Err(Error::Integrity(
                        "garbage collection would orphan a child".into(),
                    ));
                }
            }
            for id in ids {
                s.connection
                    .execute("DELETE FROM blob_literals WHERE node_id=?1", [id])
                    .map_err(db_error)?;
                s.connection
                    .execute("DELETE FROM nodes WHERE id=?1", [id])
                    .map_err(db_error)?;
            }
            Ok(())
        })
    }
    fn compact(&mut self) -> Result<usize> {
        let removed = self.transaction(|s| {
            let rows: Vec<(String, String)> = s
                .connection
                .prepare("SELECT id,payload FROM nodes")
                .map_err(db_error)?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
                .map_err(db_error)?
                .collect::<std::result::Result<_, _>>()
                .map_err(db_error)?;
            let mut referenced = BTreeSet::new();
            for (id, payload) in rows {
                let literals: BTreeSet<String> = s
                    .connection
                    .prepare("SELECT path FROM blob_literals WHERE node_id=?1")
                    .map_err(db_error)?
                    .query_map([id], |r| r.get(0))
                    .map_err(db_error)?
                    .collect::<std::result::Result<_, _>>()
                    .map_err(db_error)?;
                collect_references(
                    &serde_json::from_str(&payload).map_err(|e| Error::Integrity(e.to_string()))?,
                    &mut Vec::new(),
                    &literals,
                    &mut referenced,
                )?;
            }
            let digests: Vec<String> = s
                .connection
                .prepare("SELECT digest FROM blobs")
                .map_err(db_error)?
                .query_map([], |r| r.get(0))
                .map_err(db_error)?
                .collect::<std::result::Result<_, _>>()
                .map_err(db_error)?;
            let mut count = 0;
            for digest in digests {
                if !referenced.contains(&digest) {
                    count += s
                        .connection
                        .execute("DELETE FROM blobs WHERE digest=?1", [digest])
                        .map_err(db_error)?;
                }
            }
            Ok(count)
        })?;
        self.connection.execute_batch("VACUUM").map_err(db_error)?;
        Ok(removed)
    }
}

/// View inside a held BEGIN IMMEDIATE transaction; methods never nest transactions.
struct LockedSQLite<'a>(&'a SQLiteStore);
impl Store for LockedSQLite<'_> {
    fn put(&mut self, node: Node) -> Result<()> {
        self.0.put_locked(node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.0.get(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.0.exists(id)
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        self.0.try_exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.0.children(id)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.0.patch_locked(id, patch)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.0.roots()
    }
}

impl RecordingStore for SQLiteStore {
    fn cache_revision(&self) -> Option<crate::StoreRevision> {
        // data_version is connection-local and changes on commits by every other
        // connection, including raw SQL and lease renewal. Never persist or compare
        // it across handles. Refuse caching inside a transaction, with triggers,
        // or with externally replaced/virtual tables: such schemas can modify
        // other nodes on a local write or expose data without a database commit.
        if !self.connection.is_autocommit() {
            return None;
        }
        let (external, unsafe_schema): (u64, bool) = self.connection.prepare_cached(
            "SELECT data_version, EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' OR sql LIKE 'CREATE VIRTUAL TABLE%') OR (SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('nodes','blobs','blob_literals')) != 3 FROM pragma_data_version"
        ).ok()?.query_row([], |row| Ok((row.get(0)?, row.get(1)?))).ok()?;
        if unsafe_schema {
            return None;
        }
        Some(crate::StoreRevision {
            local: self.local_revision.get(),
            external,
        })
    }
    fn supports_reservations(&self) -> bool {
        !self.read_only
    }
    fn reserve_budget(
        &mut self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease_seconds: f64,
    ) -> Result<Option<ReservationCheck>> {
        if windows.is_empty()
            && budgets
                .iter()
                .all(|b| b.limits.keys().all(|k| k == "depth"))
        {
            return Ok(None);
        }
        self.reserve(id, budgets, windows, lease_seconds).map(Some)
    }
    fn settle_budget(&mut self, id: &str, charges: &BTreeMap<String, Decimal>) -> Result<()> {
        self.settle(id, charges)
    }
    fn release_budget(&mut self, id: &str) -> Result<()> {
        self.release(id)
    }
    fn lease_renewer(&self) -> Option<crate::store::LeaseRenewer> {
        if self.read_only {
            return None;
        }
        if self.in_memory {
            return Some(std::sync::Arc::new(|_, _| Ok(true)));
        }
        let path = self.path.clone();
        Some(std::sync::Arc::new(move |id, seconds| {
            // Existing file only, with no CREATE flag: renewal never repairs or
            // recreates a removed recording, including concurrent file removal.
            let connection = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE)
                .map_err(db_error)?;
            connection
                .busy_timeout(Duration::from_secs(30))
                .map_err(db_error)?;
            let version: Option<String> = connection
                .query_row("SELECT v FROM kv WHERE k='schema_version'", [], |r| {
                    r.get(0)
                })
                .optional()
                .map_err(db_error)?;
            if version.as_deref() != Some("3") {
                return Err(Error::Integrity("reservation schema changed".into()));
            }
            let mut store = SQLiteStore {
                connection,
                read_only: false,
                intern_threshold: None,
                in_memory: false,
                path: path.clone(),
                local_revision: Cell::new(0),
            };
            store.renew(id, seconds)
        }))
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        node.validate()?;
        self.transaction(|s| {
            let old = s.get(&node.id)?;
            if old.meta.get("state").and_then(Value::as_str) != Some("pending")
                || old.result_text.is_some()
                || !same_identity(&old, &node)
            {
                return Err(Error::Integrity(
                    "only the same pending identity may be finalized".into(),
                ));
            }
            if !["completed", "failed"]
                .contains(&node.meta.get("state").and_then(Value::as_str).unwrap_or(""))
            {
                return Err(Error::Integrity(
                    "finalized state must be completed or failed".into(),
                ));
            }
            s.connection
                .execute(
                    "UPDATE nodes SET result=?1,result_digest=?2,meta=?3 WHERE id=?4",
                    params![
                        node.result_text,
                        node.result_digest,
                        encode(&node.meta)?,
                        node.id
                    ],
                )
                .map_err(db_error)?;
            Ok(())
        })
    }
}

pub(crate) fn blob_digest(value: &Value) -> Option<&str> {
    let obj = value.as_object()?;
    if obj.len() != 1 {
        return None;
    }
    obj.get("__pollard_ref")?.as_str().filter(|s| hex64(s))
}
pub(crate) fn intern(
    value: Value,
    path: &mut Vec<Value>,
    threshold: Option<usize>,
    blobs: &mut BTreeMap<String, String>,
    literals: &mut Vec<String>,
) -> Result<Value> {
    if blob_digest(&value).is_some() {
        literals.push(encode(&json!(path))?);
        return Ok(value);
    }
    match value {
        Value::String(value) if threshold.is_some_and(|n| value.len() >= n) => {
            let digest = hash(b"", value.as_bytes());
            blobs.insert(digest.clone(), value);
            Ok(json!({"__pollard_ref":digest}))
        }
        Value::Object(values) => {
            let mut out = serde_json::Map::new();
            for (key, value) in values {
                path.push(json!(key));
                let value = intern(value, path, threshold, blobs, literals)?;
                path.pop();
                out.insert(key, value);
            }
            Ok(Value::Object(out))
        }
        Value::Array(values) => {
            let mut out = Vec::new();
            for (index, value) in values.into_iter().enumerate() {
                path.push(json!(index));
                out.push(intern(value, path, threshold, blobs, literals)?);
                path.pop();
            }
            Ok(Value::Array(out))
        }
        value => Ok(value),
    }
}
pub(crate) fn collect_references(
    value: &Value,
    path: &mut Vec<Value>,
    literals: &BTreeSet<String>,
    out: &mut BTreeSet<String>,
) -> Result<()> {
    if let Some(digest) = blob_digest(value) {
        if !literals.contains(&encode(&json!(path))?) {
            out.insert(digest.into());
        }
        return Ok(());
    }
    if let Some(values) = value.as_object() {
        for (key, value) in values {
            path.push(json!(key));
            collect_references(value, path, literals, out)?;
            path.pop();
        }
    }
    if let Some(values) = value.as_array() {
        for (index, value) in values.iter().enumerate() {
            path.push(json!(index));
            collect_references(value, path, literals, out)?;
            path.pop();
        }
    }
    Ok(())
}

fn epoch() -> Result<f64> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .map_err(|e| Error::Invalid(e.to_string()))
}
fn validate_lease(seconds: f64) -> Result<()> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(Error::Invalid(
            "lease_seconds must be finite and positive".into(),
        ));
    }
    Ok(())
}
fn parse_decimal(value: &str) -> Result<Decimal> {
    let amount = crate::parse_decimal_exact(value)
        .map_err(|e| Error::Integrity(format!("invalid stored charge: {e}")))?;
    if amount < Decimal::ZERO {
        return Err(Error::Integrity("stored charge must be nonnegative".into()));
    }
    Ok(amount)
}
fn sum_decimals(values: Vec<String>) -> Result<Decimal> {
    values.iter().try_fold(Decimal::ZERO, |total, value| {
        crate::decimal::exact_add(total, parse_decimal(value)?)
            .ok_or_else(|| Error::Integrity("stored charges overflow".into()))
    })
}
