//! Native PostgreSQL schema-v2 storage compatible with Python Pollard 1.6.0.
//!
//! The client is optional. Connection factories support caller-configured TLS;
//! the convenience constructor uses the driver's `NoTls` connector. Database
//! credentials are never retained in Debug output or included in errors.
use crate::identity::hash;
use crate::sqlite::{blob_digest, collect_references, intern, same_identity};
use crate::{
    canonical_bytes, BudgetReservation, Error, Node, NodeKind, RecordingStore, ReservationCheck,
    Result, Store, WindowReservation,
};
use postgres::{types::ToSql, Client, NoTls, Row};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error as StdError;
use std::sync::Arc;

pub const POSTGRES_SCHEMA_VERSION: i32 = 2;
type Connector = Arc<dyn Fn() -> Result<Client> + Send + Sync>;

#[derive(Debug, Clone)]
pub struct PostgresOptions {
    pub store_id: String,
    pub read_only: bool,
    /// False validates the existing schema and never creates missing tables.
    pub create: bool,
    pub intern_threshold: Option<usize>,
}
impl Default for PostgresOptions {
    fn default() -> Self {
        Self {
            store_id: "default".into(),
            read_only: false,
            create: true,
            intern_threshold: Some(1024),
        }
    }
}

pub struct PostgresStore {
    client: RefCell<Client>,
    connector: Connector,
    options: PostgresOptions,
    in_transaction: Cell<bool>,
}

const STATE_SCHEMA: &str = "CREATE TABLE pollard_reservation_state (
store_id TEXT NOT NULL,reservation_id TEXT NOT NULL,request_digest TEXT NOT NULL,request TEXT NOT NULL,
state TEXT NOT NULL CHECK(state IN ('active','settled','released')),charges_digest TEXT,charges TEXT,
expires_at DOUBLE PRECISION NOT NULL,created_at DOUBLE PRECISION NOT NULL,completed_at DOUBLE PRECISION,
PRIMARY KEY(store_id,reservation_id));";
const BASE_SCHEMA: &str = "
CREATE TABLE pollard_nodes(store_id TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,kind TEXT NOT NULL,attempt INTEGER NOT NULL,payload TEXT NOT NULL,result TEXT,result_digest TEXT,meta TEXT NOT NULL,PRIMARY KEY(store_id,id));
CREATE INDEX pollard_nodes_parent_idx ON pollard_nodes(store_id,parent);
CREATE TABLE pollard_blobs(store_id TEXT NOT NULL,digest TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(store_id,digest));
CREATE TABLE pollard_blob_literals(store_id TEXT NOT NULL,node_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(store_id,node_id,path));
CREATE TABLE pollard_budget_state(store_id TEXT NOT NULL,scope_id TEXT NOT NULL,meter TEXT NOT NULL,settled NUMERIC NOT NULL,PRIMARY KEY(store_id,scope_id,meter));
CREATE TABLE pollard_reservations(store_id TEXT NOT NULL,reservation_id TEXT NOT NULL,kind TEXT NOT NULL,scope_id TEXT NOT NULL,meter TEXT NOT NULL,amount NUMERIC NOT NULL,expires_at DOUBLE PRECISION NOT NULL,window_seconds DOUBLE PRECISION,PRIMARY KEY(store_id,reservation_id,kind,scope_id,meter));
CREATE INDEX pollard_reservations_scope_idx ON pollard_reservations(store_id,kind,scope_id,meter,expires_at);
CREATE TABLE pollard_window_scopes(store_id TEXT NOT NULL,ledger_key TEXT NOT NULL,PRIMARY KEY(store_id,ledger_key));
CREATE TABLE pollard_window_events(event_id BIGSERIAL PRIMARY KEY,store_id TEXT NOT NULL,scope_id TEXT NOT NULL,meter TEXT NOT NULL,amount NUMERIC NOT NULL,settled_at DOUBLE PRECISION NOT NULL);
CREATE INDEX pollard_window_events_scope_idx ON pollard_window_events(store_id,scope_id,meter,settled_at);";
const VERSION_SCHEMA: &str = "CREATE TABLE pollard_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);INSERT INTO pollard_schema(singleton,version) VALUES(1,2);";
const LAYOUT: &[(&str, &[&str])] = &[
    (
        "pollard_nodes",
        &[
            "store_id",
            "id",
            "parent",
            "kind",
            "attempt",
            "payload",
            "result",
            "result_digest",
            "meta",
        ],
    ),
    ("pollard_blobs", &["store_id", "digest", "value"]),
    ("pollard_blob_literals", &["store_id", "node_id", "path"]),
    (
        "pollard_budget_state",
        &["store_id", "scope_id", "meter", "settled"],
    ),
    (
        "pollard_reservations",
        &[
            "store_id",
            "reservation_id",
            "kind",
            "scope_id",
            "meter",
            "amount",
            "expires_at",
            "window_seconds",
        ],
    ),
    ("pollard_window_scopes", &["store_id", "ledger_key"]),
    (
        "pollard_window_events",
        &[
            "event_id",
            "store_id",
            "scope_id",
            "meter",
            "amount",
            "settled_at",
        ],
    ),
    (
        "pollard_reservation_state",
        &[
            "store_id",
            "reservation_id",
            "request_digest",
            "request",
            "state",
            "charges_digest",
            "charges",
            "expires_at",
            "created_at",
            "completed_at",
        ],
    ),
    ("pollard_schema", &["singleton", "version", "updated_at"]),
];

fn backend_error(error: postgres::Error) -> Error {
    let mut cause = error.source();
    let mut connection_lost = error.is_closed();
    while let Some(next) = cause {
        connection_lost |= next.is::<std::io::Error>();
        cause = next.source();
    }
    if let Some(db) = error.as_db_error() {
        connection_lost |= db.code().code().starts_with("08")
            || ["57P01", "57P02", "57P03"].contains(&db.code().code());
        // Server primary messages have no connection-string/password material.
        Error::Backend {
            detail: format!("PostgreSQL {}: {}", db.code().code(), db.message()),
            connection_lost,
        }
    } else {
        Error::Backend {
            detail: format!("PostgreSQL: {error}"),
            connection_lost,
        }
    }
}
fn is_connection_error(error: &Error) -> bool {
    matches!(
        error,
        Error::Backend {
            connection_lost: true,
            ..
        }
    )
}
fn text(value: &Value) -> Result<String> {
    Ok(crate::result_text_and_digest(value)?.0)
}
fn parse(value: &str) -> Result<Value> {
    serde_json::from_str(value).map_err(|e| Error::Integrity(format!("PostgreSQL JSON: {e}")))
}
fn decimal(value: &str) -> Result<Decimal> {
    let value = crate::parse_decimal_exact(value).map_err(|_| {
        Error::Integrity("PostgreSQL amount is outside supported exact decimal range".into())
    })?;
    if value < Decimal::ZERO {
        return Err(Error::Integrity("negative stored PostgreSQL amount".into()));
    }
    Ok(value)
}
fn add(a: Decimal, b: Decimal) -> Result<Decimal> {
    crate::decimal::exact_add(a, b).ok_or_else(|| Error::Integrity("decimal overflow".into()))
}
fn subtract(a: Decimal, b: Decimal) -> Result<Decimal> {
    crate::decimal::exact_subtract(a, b).ok_or_else(|| Error::Integrity("decimal overflow".into()))
}

impl PostgresStore {
    pub fn connect(conninfo: impl Into<String>) -> Result<Self> {
        Self::connect_with_options(conninfo, PostgresOptions::default())
    }
    pub fn connect_with_options(
        conninfo: impl Into<String>,
        options: PostgresOptions,
    ) -> Result<Self> {
        let conninfo = conninfo.into();
        Self::connect_with_factory(
            move || Client::connect(&conninfo, NoTls).map_err(backend_error),
            options,
        )
    }
    /// The factory must return an independent connection, including for lease
    /// heartbeats. This permits the real postgres driver with a custom TLS connector.
    pub fn connect_with_factory(
        factory: impl Fn() -> Result<Client> + Send + Sync + 'static,
        options: PostgresOptions,
    ) -> Result<Self> {
        if options.store_id.is_empty() || options.intern_threshold == Some(0) {
            return Err(Error::Invalid(
                "store_id and positive interning threshold are required".into(),
            ));
        }
        let connector: Connector = Arc::new(factory);
        let store = Self {
            client: RefCell::new(connector()?),
            connector,
            options,
            in_transaction: Cell::new(false),
        };
        if store.options.read_only {
            store.batch("SET default_transaction_read_only=on")?;
        }
        store.transaction(false, || {
            if store.options.create && !store.options.read_only {
                store.query(
                    "SELECT pg_advisory_xact_lock(hashtextextended('pollard-schema',0))",
                    &[],
                )?;
                if store.version()?.is_none() {
                    let existing = store.existing_tables()?;
                    if !existing.is_empty() {
                        return Err(Error::UnsupportedSchema(
                            "PostgreSQL legacy or partial schema requires explicit migration"
                                .into(),
                        ));
                    }
                    store.batch(BASE_SCHEMA)?;
                    store.batch(STATE_SCHEMA)?;
                    store.batch(VERSION_SCHEMA)?;
                }
            }
            store.require_schema()
        })?;
        Ok(store)
    }
    pub fn is_read_only(&self) -> bool {
        self.options.read_only
    }
    pub fn store_id(&self) -> &str {
        &self.options.store_id
    }
    /// Validate a candidate before atomically replacing the current connection.
    /// Reconnect never creates or repairs a missing schema.
    pub fn reconnect(&self) -> Result<()> {
        if self.in_transaction.get() {
            return Err(Error::Busy);
        }
        let candidate = Self {
            client: RefCell::new((self.connector)()?),
            connector: self.connector.clone(),
            options: self.options.clone(),
            in_transaction: Cell::new(false),
        };
        if candidate.options.read_only {
            candidate.batch("SET default_transaction_read_only=on")?;
        }
        candidate.transaction(false, || candidate.require_schema())?;
        self.client.replace(candidate.client.into_inner());
        Ok(())
    }
    fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Result<Vec<Row>> {
        self.client
            .borrow_mut()
            .query(sql, params)
            .map_err(backend_error)
    }
    fn execute(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Result<u64> {
        self.client
            .borrow_mut()
            .execute(sql, params)
            .map_err(backend_error)
    }
    fn batch(&self, sql: &str) -> Result<()> {
        self.client
            .borrow_mut()
            .batch_execute(sql)
            .map_err(backend_error)
    }
    fn transaction<T>(&self, write: bool, callback: impl FnOnce() -> Result<T>) -> Result<T> {
        if write && self.options.read_only {
            return Err(Error::Invalid("PostgreSQL store is read-only".into()));
        }
        if self.in_transaction.get() {
            return callback();
        }
        self.batch("BEGIN")?;
        self.in_transaction.set(true);
        let mut guard = TransactionGuard {
            store: self,
            active: true,
        };
        let result = callback()?;
        self.batch("COMMIT")?;
        guard.active = false;
        self.in_transaction.set(false);
        Ok(result)
    }
    fn version(&self) -> Result<Option<i32>> {
        let present: bool =
            self.query("SELECT to_regclass('pollard_schema') IS NOT NULL", &[])?[0].get(0);
        if !present {
            return Ok(None);
        }
        let rows = self.query(
            "SELECT singleton,version FROM pollard_schema ORDER BY singleton",
            &[],
        )?;
        if rows.len() != 1 || rows[0].get::<_, i32>(0) != 1 {
            return Err(Error::UnsupportedSchema(
                "invalid PostgreSQL schema version record".into(),
            ));
        }
        Ok(Some(rows[0].get(1)))
    }
    fn existing_tables(&self) -> Result<BTreeSet<String>> {
        let mut result = BTreeSet::new();
        for (name, _) in LAYOUT {
            if self.query("SELECT to_regclass($1) IS NOT NULL", &[name])?[0].get::<_, bool>(0) {
                result.insert((*name).into());
            }
        }
        Ok(result)
    }
    fn require_schema(&self) -> Result<()> {
        if self.version()? != Some(POSTGRES_SCHEMA_VERSION) {
            return Err(Error::UnsupportedSchema("PostgreSQL schema version 2 required; run explicit migration for a drained legacy database".into()));
        }
        self.require_layout(LAYOUT)
    }
    fn require_layout(&self, expected: &[(&str, &[&str])]) -> Result<()> {
        for (table, fields) in expected {
            let actual: BTreeSet<String> = self.query("SELECT attname::text FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass($1) AND attnum>0 AND NOT attisdropped", &[table])?.into_iter().map(|r| r.get(0)).collect();
            let wanted: BTreeSet<String> = fields.iter().map(|f| (*f).into()).collect();
            if actual != wanted {
                return Err(Error::UnsupportedSchema(format!(
                    "unsupported PostgreSQL table layout: {table}"
                )));
            }
        }
        Ok(())
    }
    fn now(&self) -> Result<f64> {
        let now: f64 = self.query(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision",
            &[],
        )?[0]
            .get(0);
        if !now.is_finite() || now < 0.0 {
            return Err(Error::Integrity("invalid PostgreSQL server clock".into()));
        }
        Ok(now)
    }
    fn node(&self, id: &str, lock: bool) -> Result<Option<Node>> {
        let sql = format!("SELECT id,parent,kind,attempt,payload,result,result_digest,meta FROM pollard_nodes WHERE store_id=$1 AND id=$2{}", if lock {" FOR UPDATE"} else {""});
        let rows = self.query(&sql, &[&self.options.store_id, &id])?;
        let Some(row) = rows.first() else {
            return Ok(None);
        };
        let attempt: i32 = row.get(3);
        if attempt < 0 {
            return Err(Error::Integrity("negative PostgreSQL attempt".into()));
        }
        let literals: BTreeSet<String> = self
            .query(
                "SELECT path FROM pollard_blob_literals WHERE store_id=$1 AND node_id=$2",
                &[&self.options.store_id, &id],
            )?
            .into_iter()
            .map(|r| r.get(0))
            .collect();
        let payload = self.rehydrate(parse(row.get::<_, &str>(4))?, &mut Vec::new(), &literals)?;
        let kind: NodeKind = serde_json::from_value(json!(row.get::<_, String>(2)))
            .map_err(|e| Error::Integrity(e.to_string()))?;
        Node::from_storage(
            row.get(0),
            row.get(1),
            kind,
            attempt as u64,
            &text(&payload)?,
            row.get(5),
            row.get(6),
            row.get::<_, &str>(7),
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
            if literals.contains(&text(&json!(path))?) {
                return Ok(value);
            }
            let rows = self.query(
                "SELECT value FROM pollard_blobs WHERE store_id=$1 AND digest=$2",
                &[&self.options.store_id, &digest],
            )?;
            let blob: String = rows
                .first()
                .ok_or_else(|| Error::Integrity("missing PostgreSQL payload blob".into()))?
                .get(0);
            if hash(b"", blob.as_bytes()) != digest {
                return Err(Error::Integrity("PostgreSQL blob digest mismatch".into()));
            }
            return Ok(json!(blob));
        }
        match value {
            Value::Object(values) => {
                let mut out = serde_json::Map::new();
                for (key, value) in values {
                    path.push(json!(key));
                    out.insert(key, self.rehydrate(value, path, literals)?);
                    path.pop();
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
    fn put_locked(&self, node: &Node) -> Result<()> {
        node.validate()?;
        let attempt = i32::try_from(node.attempt).map_err(|_| {
            Error::Invalid("PostgreSQL schema attempt exceeds INTEGER range".into())
        })?;
        if let Some(parent) = &node.parent {
            if !self.try_exists(parent)? {
                return Err(Error::NotFound(parent.clone()));
            }
        }
        let mut blobs = BTreeMap::new();
        let mut literals = Vec::new();
        let payload = intern(
            node.payload.clone(),
            &mut Vec::new(),
            self.options.intern_threshold,
            &mut blobs,
            &mut literals,
        )?;
        for (digest, value) in blobs {
            self.execute("INSERT INTO pollard_blobs(store_id,digest,value) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", &[&self.options.store_id,&digest,&value])?;
            let existing: String = self.query(
                "SELECT value FROM pollard_blobs WHERE store_id=$1 AND digest=$2",
                &[&self.options.store_id, &digest],
            )?[0]
                .get(0);
            if existing != value {
                return Err(Error::Integrity("PostgreSQL payload blob collision".into()));
            }
        }
        let inserted = self.execute("INSERT INTO pollard_nodes(store_id,id,parent,kind,attempt,payload,result,result_digest,meta) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING", &[&self.options.store_id,&node.id,&node.parent,&node.kind.as_str(),&attempt,&text(&payload)?,&node.result_text,&node.result_digest,&text(&node.meta)?])?;
        if inserted == 0 {
            let mut existing = self
                .node(&node.id, true)?
                .ok_or_else(|| Error::Integrity("node disappeared during put".into()))?;
            if !same_identity(&existing, node) {
                return Err(Error::Integrity(
                    "PostgreSQL node identity collision".into(),
                ));
            }
            if node.result_text.is_some() && node.result_text != existing.result_text {
                let conflict = json!({"result_digest":node.result_digest,"result":node.result});
                let conflicts = existing
                    .meta
                    .as_object_mut()
                    .ok_or_else(|| Error::Integrity("invalid stored metadata".into()))?
                    .entry("result_conflicts")
                    .or_insert_with(|| json!([]))
                    .as_array_mut()
                    .ok_or_else(|| Error::Integrity("invalid result_conflicts".into()))?;
                if !conflicts
                    .iter()
                    .any(|v| crate::identity::result_values_equal(v, &conflict))
                {
                    conflicts.push(conflict);
                    self.write_meta(&node.id, &existing.meta)?;
                }
            }
        } else {
            for path in literals {
                self.execute("INSERT INTO pollard_blob_literals(store_id,node_id,path) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", &[&self.options.store_id,&node.id,&path])?;
            }
        }
        Ok(())
    }
    fn write_meta(&self, id: &str, meta: &Value) -> Result<()> {
        crate::identity::validate_finite_json(meta)?;
        self.execute(
            "UPDATE pollard_nodes SET meta=$3 WHERE store_id=$1 AND id=$2",
            &[&self.options.store_id, &id, &text(meta)?],
        )?;
        Ok(())
    }
    fn patch_locked(&self, id: &str, patch: &Value) -> Result<()> {
        let mut node = self
            .node(id, true)?
            .ok_or_else(|| Error::NotFound(id.into()))?;
        node.meta
            .as_object_mut()
            .ok_or_else(|| Error::Integrity("invalid stored metadata".into()))?
            .extend(
                patch
                    .as_object()
                    .ok_or_else(|| Error::Invalid("metadata patch must be object".into()))?
                    .clone(),
            );
        self.write_meta(id, &node.meta)
    }
    fn retry<T>(&self, mut callback: impl FnMut() -> Result<T>) -> Result<T> {
        match callback() {
            Err(error) if is_connection_error(&error) && !self.in_transaction.get() => {
                self.reconnect()?;
                callback()
            }
            result => result,
        }
    }
    fn node_ids(&self, parent: Option<&str>) -> Result<Vec<String>> {
        let rows = if let Some(parent) = parent {
            self.query("SELECT id FROM pollard_nodes WHERE store_id=$1 AND parent=$2 ORDER BY kind COLLATE \"C\",id COLLATE \"C\"", &[&self.options.store_id,&parent])?
        } else {
            self.query(
                "SELECT id FROM pollard_nodes WHERE store_id=$1 AND parent IS NULL",
                &[&self.options.store_id],
            )?
        };
        let mut ids: Vec<String> = rows.into_iter().map(|row| row.get(0)).collect();
        if parent.is_none() {
            let mut roots = Vec::new();
            for id in ids {
                let node = self
                    .node(&id, false)?
                    .ok_or_else(|| Error::NotFound(id.clone()))?;
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
            ids = roots.into_iter().map(|(_, id)| id).collect();
        }
        Ok(ids)
    }
    /// Explicitly migrate a backed-up database after stopping all workers.
    /// Any legacy reservation row, including expired rows, blocks migration.
    pub fn migrate(conninfo: impl Into<String>) -> Result<(i32, i32)> {
        let conninfo = conninfo.into();
        let connector: Connector =
            Arc::new(move || Client::connect(&conninfo, NoTls).map_err(backend_error));
        let store = Self {
            client: RefCell::new(connector()?),
            connector,
            options: PostgresOptions::default(),
            in_transaction: Cell::new(false),
        };
        store.transaction(true,||{
            store.query("SELECT pg_advisory_xact_lock(hashtextextended('pollard-schema',0))",&[])?;
            let version=store.version()?;
            let original=version.unwrap_or(0);
            if version==Some(2) {store.require_schema()?;return Ok((2,2));}
            if !matches!(version,None|Some(1)){return Err(Error::UnsupportedSchema("unsupported PostgreSQL migration version".into()));}
            let existing=store.existing_tables()?;
            if existing.is_empty(){store.batch(BASE_SCHEMA)?;store.batch(STATE_SCHEMA)?;store.batch(VERSION_SCHEMA)?;return Ok((0,2));}
            let legacy:BTreeSet<String>=LAYOUT[..7].iter().map(|(name,_)|(*name).into()).collect();
            let expected=if version==Some(1){let mut values=legacy.clone();values.insert("pollard_schema".into());values}else{legacy};
            if existing!=expected{return Err(Error::UnsupportedSchema("partial PostgreSQL migration schema".into()));}
            store.require_layout(&LAYOUT[..7])?;
            if store.query("SELECT COUNT(*) FROM pollard_reservations",&[])?[0].get::<_,i64>(0)!=0{return Err(Error::Integrity("PostgreSQL migration requires empty reservations and drained workers".into()));}
            store.batch(STATE_SCHEMA)?;
            if version.is_none(){store.batch(VERSION_SCHEMA)?;}else{store.execute("UPDATE pollard_schema SET version=2,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",&[])?;}
            store.require_schema()?;Ok((original,2))
        })
    }
}

impl Store for PostgresStore {
    fn put(&mut self, node: Node) -> Result<()> {
        self.retry(|| self.transaction(true, || self.put_locked(&node)))
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.retry(|| {
            self.node(id, false)?
                .ok_or_else(|| Error::NotFound(id.into()))
        })
    }
    fn exists(&self, id: &str) -> bool {
        self.try_exists(id)
            .expect("PostgreSQL existence query failed; use try_exists to handle database errors")
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        self.retry(|| {
            Ok(!self
                .query(
                    "SELECT 1 FROM pollard_nodes WHERE store_id=$1 AND id=$2",
                    &[&self.options.store_id, &id],
                )?
                .is_empty())
        })
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.retry(|| self.node_ids(Some(id)))
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.retry(|| self.node_ids(None))
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.retry(|| self.transaction(true, || self.patch_locked(id, &patch)))
    }
    fn import_nodes(&mut self, nodes: Vec<Node>) -> Result<(usize, usize)> {
        self.retry(|| {
            self.transaction(true, || {
                self.batch("LOCK TABLE pollard_nodes IN SHARE ROW EXCLUSIVE MODE")?;
                crate::merge::import_prepared(&mut LockedPostgres(self), nodes.clone())
            })
        })
    }
    fn merge_nodes(&mut self, nodes: Vec<Node>, replay: bool) -> Result<crate::MergeReport> {
        self.retry(|| {
            self.transaction(true, || {
                self.batch("LOCK TABLE pollard_nodes IN SHARE ROW EXCLUSIVE MODE")?;
                crate::merge::merge_prepared(&mut LockedPostgres(self), nodes.clone(), replay)
            })
        })
    }
    fn apply_batch(&mut self, nodes: Vec<Node>, patches: Vec<(String, Value)>) -> Result<()> {
        self.retry(|| {
            self.transaction(true, || {
                for node in &nodes {
                    self.put_locked(node)?;
                }
                for (id, patch) in &patches {
                    self.patch_locked(id, patch)?;
                }
                Ok(())
            })
        })
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.retry(|| {
            self.transaction(true, || {
                self.batch("LOCK TABLE pollard_nodes IN SHARE ROW EXCLUSIVE MODE")?;
                for id in ids {
                    self.execute(
                        "DELETE FROM pollard_blob_literals WHERE store_id=$1 AND node_id=$2",
                        &[&self.options.store_id, &id],
                    )?;
                    self.execute(
                        "DELETE FROM pollard_nodes WHERE store_id=$1 AND id=$2",
                        &[&self.options.store_id, &id],
                    )?;
                }
                Ok(())
            })
        })
    }
    fn compact(&mut self) -> Result<usize> {
        self.retry(||self.transaction(true,||{
            self.batch("LOCK TABLE pollard_nodes IN SHARE ROW EXCLUSIVE MODE")?;
            let mut used=BTreeSet::new();
            for row in self.query("SELECT id,payload FROM pollard_nodes WHERE store_id=$1",&[&self.options.store_id])?{
                let id:String=row.get(0);
                let literals:BTreeSet<String>=self.query("SELECT path FROM pollard_blob_literals WHERE store_id=$1 AND node_id=$2",&[&self.options.store_id,&id])?.into_iter().map(|r|r.get(0)).collect();
                collect_references(&parse(row.get::<_,&str>(1))?,&mut Vec::new(),&literals,&mut used)?;
            }
            let mut removed=0;
            for row in self.query("SELECT digest FROM pollard_blobs WHERE store_id=$1",&[&self.options.store_id])?{
                let digest:String=row.get(0);
                if !used.contains(&digest){removed+=self.execute("DELETE FROM pollard_blobs WHERE store_id=$1 AND digest=$2",&[&self.options.store_id,&digest])? as usize;}
            }Ok(removed)
        }))
    }
}

struct LockedPostgres<'a>(&'a PostgresStore);
impl Store for LockedPostgres<'_> {
    fn put(&mut self, node: Node) -> Result<()> {
        self.0.put_locked(&node)
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.0
            .node(id, true)?
            .ok_or_else(|| Error::NotFound(id.into()))
    }
    fn exists(&self, id: &str) -> bool {
        self.try_exists(id)
            .expect("PostgreSQL existence query failed")
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        Ok(!self
            .0
            .query(
                "SELECT 1 FROM pollard_nodes WHERE store_id=$1 AND id=$2",
                &[&self.0.options.store_id, &id],
            )?
            .is_empty())
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.0.node_ids(Some(id))
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.0.node_ids(None)
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.0.patch_locked(id, &patch)
    }
}

impl RecordingStore for PostgresStore {
    fn supports_reservations(&self) -> bool {
        !self.options.read_only
    }
    fn reserve_budget(
        &mut self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease: f64,
    ) -> Result<Option<ReservationCheck>> {
        self.reserve(id, budgets, windows, lease).map(Some)
    }
    fn settle_budget(&mut self, id: &str, charges: &BTreeMap<String, Decimal>) -> Result<()> {
        self.settle(id, charges)
    }
    fn release_budget(&mut self, id: &str) -> Result<()> {
        self.release(id)
    }
    fn lease_renewer(&self) -> Option<crate::LeaseRenewer> {
        if self.options.read_only {
            return None;
        }
        let connector = self.connector.clone();
        let mut options = self.options.clone();
        options.create = false;
        Some(Arc::new(move |id, seconds| {
            let factory = connector.clone();
            let store = PostgresStore::connect_with_factory(move || factory(), options.clone())?;
            store.renew(id, seconds)
        }))
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        node.validate()?;
        self.retry(||self.transaction(true,||{
            let existing=self.node(&node.id,true)?.ok_or_else(||Error::NotFound(node.id.clone()))?;
            if existing==node{return Ok(());}
            if existing.result_text.is_some() || existing.meta.get("state").and_then(Value::as_str)!=Some("pending") || !same_identity(&existing,&node){return Err(Error::Integrity("PostgreSQL finalization requires matching pending identity".into()));}
            self.execute("UPDATE pollard_nodes SET result=$3,result_digest=$4,meta=$5 WHERE store_id=$1 AND id=$2",&[&self.options.store_id,&node.id,&node.result_text,&node.result_digest,&text(&node.meta)?])?;
            Ok(())
        }))
    }
}

fn float_text(value: f64) -> Result<String> {
    Ok(crate::result_text_and_digest(&json!(value))?.0)
}
fn lease_valid(value: f64) -> Result<()> {
    if value.is_finite() && value > 0.0 {
        Ok(())
    } else {
        Err(Error::Invalid("lease must be finite and positive".into()))
    }
}
fn amounts(values: &BTreeMap<String, Decimal>) -> Value {
    json!(values
        .iter()
        .map(|(k, v)| (k.clone(), crate::kv::decimal_string(*v)))
        .collect::<BTreeMap<_, _>>())
}
fn request_text(
    budgets: &[BudgetReservation],
    windows: &[WindowReservation],
    lease: f64,
) -> Result<(String, String)> {
    lease_valid(lease)?;
    let mut keys = BTreeSet::new();
    let mut budget_order: Vec<_> = budgets.iter().collect();
    budget_order.sort_by_key(|b| &b.scope_id);
    let mut window_order: Vec<_> = windows.iter().collect();
    window_order.sort_by_key(|w| &w.ledger_key);
    for b in budgets {
        if b.scope_id.is_empty() {
            return Err(Error::Invalid("empty budget scope".into()));
        }
        for values in [&b.limits, &b.baseline, &b.estimates] {
            for (k, v) in values {
                if k.is_empty() || *v < Decimal::ZERO {
                    return Err(Error::Invalid(
                        "invalid budget meter or negative amount".into(),
                    ));
                }
            }
        }
        for meter in b.limits.keys() {
            if !keys.insert(("budget", b.scope_id.as_str(), meter.as_str())) {
                return Err(Error::Invalid("duplicate budget scope meter".into()));
            }
        }
    }
    for w in windows {
        lease_valid(w.window_seconds)?;
        if w.ledger_key.is_empty()
            || w.meter.is_empty()
            || w.limit < Decimal::ZERO
            || w.amount < Decimal::ZERO
            || !keys.insert(("window", w.ledger_key.as_str(), w.meter.as_str()))
        {
            return Err(Error::Invalid("invalid window reservation".into()));
        }
    }
    let document = json!({"budgets":budget_order.into_iter().map(|b|json!({"scope_id":b.scope_id,"limits":amounts(&b.limits),"baseline":amounts(&b.baseline),"estimates":amounts(&b.estimates)})).collect::<Vec<_>>(),
        "windows":window_order.into_iter().map(|w|Ok(json!({"ledger_key":w.ledger_key,"meter":w.meter,"limit":crate::kv::decimal_string(w.limit),"amount":crate::kv::decimal_string(w.amount),"window_seconds":float_text(w.window_seconds)?}))).collect::<Result<Vec<_>>>()?,"lease_seconds":float_text(lease)?});
    let bytes = canonical_bytes(&document)?;
    let digest = hash(b"", &bytes);
    Ok((String::from_utf8(bytes).expect("UTF-8 JSON"), digest))
}
fn charges_text(charges: &BTreeMap<String, Decimal>) -> Result<(String, String)> {
    if charges
        .iter()
        .any(|(k, v)| k.is_empty() || *v < Decimal::ZERO)
    {
        return Err(Error::Invalid(
            "charges require nonempty meters and nonnegative values".into(),
        ));
    }
    let bytes = canonical_bytes(&amounts(charges))?;
    let digest = hash(b"", &bytes);
    Ok((String::from_utf8(bytes).expect("UTF-8 JSON"), digest))
}

impl PostgresStore {
    fn reservation_retry<T>(
        &self,
        id: &str,
        settlement: bool,
        mut callback: impl FnMut() -> Result<T>,
    ) -> Result<T> {
        match callback() {
            Err(error) if is_connection_error(&error) => {}
            result => return result,
        }
        match self.reconnect().and_then(|()| callback()) {
            Err(error) if is_connection_error(&error) => {
                if settlement {
                    Err(Error::SettlementUncertain {
                        reservation_id: id.into(),
                    })
                } else {
                    Err(Error::ReservationUncertain {
                        reservation_id: id.into(),
                    })
                }
            }
            result => result,
        }
    }
    fn sum(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Result<Decimal> {
        self.query(sql, params)?
            .into_iter()
            .try_fold(Decimal::ZERO, |total, row| {
                add(total, decimal(row.get::<_, &str>(0))?)
            })
    }
    pub fn reserve(
        &self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease: f64,
    ) -> Result<ReservationCheck> {
        if id.is_empty() {
            return Err(Error::Invalid("reservation_id must be nonempty".into()));
        }
        let (request, digest) = request_text(budgets, windows, lease)?;
        self.reservation_retry(id, false, || {
            self.transaction(true, || {
                self.reserve_once(id, budgets, windows, lease, &request, &digest)
            })
        })
    }
    fn reserve_once(
        &self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease: f64,
        request: &str,
        digest: &str,
    ) -> Result<ReservationCheck> {
        let store = &self.options.store_id;
        let previous=self.query("SELECT request_digest,state,expires_at FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2 FOR UPDATE",&[store,&id])?;
        if let Some(row) = previous.first() {
            if row.get::<_, &str>(0) != digest {
                return Err(Error::Integrity(format!(
                    "reservation retry changed request: {id}"
                )));
            }
            let state: &str = row.get(1);
            if state != "active" {
                return Err(Error::Integrity(format!(
                    "reservation is already {state}: {id}"
                )));
            }
            if row.get::<_, f64>(2) <= self.now()? {
                return Err(Error::Integrity(format!(
                    "reservation expired before retry: {id}"
                )));
            }
            return Ok(ReservationCheck::default());
        }
        let mut budget_rows = Vec::new();
        for budget in budgets {
            for (meter, limit) in &budget.limits {
                if meter != "depth" {
                    budget_rows.push((budget, meter, *limit));
                }
            }
        }
        budget_rows.sort_by(|a, b| (&a.0.scope_id, a.1).cmp(&(&b.0.scope_id, b.1)));
        for (budget, meter, _) in &budget_rows {
            let baseline = budget
                .baseline
                .get(*meter)
                .copied()
                .unwrap_or_default()
                .to_string();
            self.execute("INSERT INTO pollard_budget_state(store_id,scope_id,meter,settled) VALUES($1,$2,$3,$4::text::numeric) ON CONFLICT DO NOTHING",&[store,&budget.scope_id,meter,&baseline])?;
        }
        for (budget, meter, _) in &budget_rows {
            self.query("SELECT settled::text FROM pollard_budget_state WHERE store_id=$1 AND scope_id=$2 AND meter=$3 FOR UPDATE",&[store,&budget.scope_id,meter])?;
        }
        let mut window_order: Vec<_> = windows.iter().collect();
        window_order.sort_by_key(|w| &w.ledger_key);
        for window in &window_order {
            self.execute("INSERT INTO pollard_window_scopes(store_id,ledger_key) VALUES($1,$2) ON CONFLICT DO NOTHING",&[store,&window.ledger_key])?;
            self.query("SELECT ledger_key FROM pollard_window_scopes WHERE store_id=$1 AND ledger_key=$2 FOR UPDATE",&[store,&window.ledger_key])?;
        }
        // Server time is sampled only after all potentially blocking locks.
        let now = self.now()?;
        for (budget, meter, limit) in &budget_rows {
            let rows=self.query("SELECT settled::text FROM pollard_budget_state WHERE store_id=$1 AND scope_id=$2 AND meter=$3",&[store,&budget.scope_id,meter])?;
            let mut settled = decimal(
                rows.first()
                    .ok_or_else(|| Error::Integrity("missing budget state".into()))?
                    .get::<_, &str>(0),
            )?;
            let baseline = budget.baseline.get(*meter).copied().unwrap_or_default();
            if baseline > settled {
                settled = baseline;
                self.execute("UPDATE pollard_budget_state SET settled=$4::text::numeric WHERE store_id=$1 AND scope_id=$2 AND meter=$3",&[store,&budget.scope_id,meter,&settled.to_string()])?;
            }
            let active=self.sum("SELECT amount::text FROM pollard_reservations WHERE store_id=$1 AND kind='budget' AND scope_id=$2 AND meter=$3 AND expires_at>$4",&[store,&budget.scope_id,meter,&now])?;
            let remaining = subtract(subtract(*limit, settled)?, active)?;
            let requested = budget.estimates.get(*meter).copied().unwrap_or_default();
            if requested > remaining {
                return Ok(ReservationCheck {
                    ok: false,
                    meter: Some((*meter).clone()),
                    requested,
                    remaining,
                    ..Default::default()
                });
            }
        }
        for window in &window_order {
            let cutoff = now - window.window_seconds;
            self.execute("DELETE FROM pollard_window_events WHERE store_id=$1 AND scope_id=$2 AND settled_at<=$3",&[store,&window.ledger_key,&cutoff])?;
            let settled=self.sum("SELECT amount::text FROM pollard_window_events WHERE store_id=$1 AND scope_id=$2 AND settled_at>$3",&[store,&window.ledger_key,&cutoff])?;
            let active=self.sum("SELECT amount::text FROM pollard_reservations WHERE store_id=$1 AND kind='window' AND scope_id=$2 AND expires_at>$3",&[store,&window.ledger_key,&now])?;
            let remaining = subtract(subtract(window.limit, settled)?, active)?;
            if window.amount > remaining {
                return Ok(ReservationCheck {
                    ok: false,
                    reason: "window".into(),
                    meter: Some(window.meter.clone()),
                    requested: window.amount,
                    remaining,
                    window_seconds: Some(window.window_seconds),
                });
            }
        }
        let expiry = now + lease;
        if !expiry.is_finite() {
            return Err(Error::Invalid("lease expiry overflow".into()));
        }
        self.execute("INSERT INTO pollard_reservation_state(store_id,reservation_id,request_digest,request,state,charges_digest,charges,expires_at,created_at,completed_at) VALUES($1,$2,$3,$4,'active',NULL,NULL,$5,$6,NULL)",&[store,&id,&digest,&request,&expiry,&now])?;
        for (budget, meter, _) in budget_rows {
            let amount = budget
                .estimates
                .get(meter)
                .copied()
                .unwrap_or_default()
                .to_string();
            self.execute("INSERT INTO pollard_reservations(store_id,reservation_id,kind,scope_id,meter,amount,expires_at,window_seconds) VALUES($1,$2,'budget',$3,$4,$5::text::numeric,$6,NULL)",&[store,&id,&budget.scope_id,&meter,&amount,&expiry])?;
        }
        for window in windows {
            self.execute("INSERT INTO pollard_reservations(store_id,reservation_id,kind,scope_id,meter,amount,expires_at,window_seconds) VALUES($1,$2,'window',$3,$4,$5::text::numeric,$6,$7)",&[store,&id,&window.ledger_key,&window.meter,&window.amount.to_string(),&expiry,&window.window_seconds])?;
        }
        Ok(ReservationCheck::default())
    }
    pub fn settle(&self, id: &str, charges: &BTreeMap<String, Decimal>) -> Result<()> {
        let (encoded, digest) = charges_text(charges)?;
        self.settle_encoded(id, charges, &encoded, &digest)
    }
    /// Reserve with the exact Python Decimal spelling used for retry fingerprints.
    pub fn reserve_decimal_text(
        &self,
        id: &str,
        budgets: &[crate::TextBudgetReservation],
        windows: &[crate::TextWindowReservation],
        lease: f64,
    ) -> Result<ReservationCheck> {
        if id.is_empty() {
            return Err(Error::Invalid("reservation_id must be nonempty".into()));
        }
        let prepared = crate::decimal_wire::prepare_request(budgets, windows, lease)?;
        request_text(&prepared.budgets, &prepared.windows, lease)?;
        self.reservation_retry(id, false, || {
            self.transaction(true, || {
                self.reserve_once(
                    id,
                    &prepared.budgets,
                    &prepared.windows,
                    lease,
                    &prepared.encoded.0,
                    &prepared.encoded.1,
                )
            })
        })
    }
    /// Settle or retry settlement without losing exponent, scale, or signed zero.
    pub fn settle_decimal_text(&self, id: &str, charges: &BTreeMap<String, String>) -> Result<()> {
        let prepared = crate::decimal_wire::prepare_charges(charges)?;
        charges_text(&prepared.amounts)?;
        self.settle_encoded(
            id,
            &prepared.amounts,
            &prepared.encoded.0,
            &prepared.encoded.1,
        )
    }
    fn settle_encoded(
        &self,
        id: &str,
        charges: &BTreeMap<String, Decimal>,
        encoded: &str,
        digest: &str,
    ) -> Result<()> {
        self.reservation_retry(id,true,||self.transaction(true,||{
            let store=&self.options.store_id;
            let states=self.query("SELECT state,charges_digest FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2 FOR UPDATE",&[store,&id])?;
            let state=states.first().ok_or_else(||Error::Integrity(format!("unknown reservation: {id}")))?;
            let status:&str=state.get(0);
            if status=="settled"{if state.get::<_,Option<&str>>(1)!=Some(digest){return Err(Error::Integrity(format!("reservation retry used different charges: {id}")));}return Ok(());}
            if status!="active"{return Err(Error::Integrity(format!("reservation is already {status}: {id}")));}
            let rows=self.query("SELECT kind,scope_id,meter FROM pollard_reservations WHERE store_id=$1 AND reservation_id=$2 ORDER BY kind,scope_id,meter FOR UPDATE",&[store,&id])?;
            if rows.is_empty(){return Err(Error::Integrity(format!("reservation details are missing: {id}")));}
            let window_scopes:BTreeSet<String>=rows.iter().filter(|r|r.get::<_,&str>(0)=="window").map(|r|r.get(1)).collect();
            for scope in window_scopes{if self.query("SELECT ledger_key FROM pollard_window_scopes WHERE store_id=$1 AND ledger_key=$2 FOR UPDATE",&[store,&scope])?.is_empty(){return Err(Error::Integrity("window scope missing during settlement".into()));}}
            let now=self.now()?;
            for row in rows{
                let(kind,scope,meter):(String,String,String)=(row.get(0),row.get(1),row.get(2));
                let actual=charges.get(&meter).copied().unwrap_or_default();
                match kind.as_str(){
                    "budget"=>{
                        let previous=self.query("SELECT settled::text FROM pollard_budget_state WHERE store_id=$1 AND scope_id=$2 AND meter=$3 FOR UPDATE",&[store,&scope,&meter])?;
                        let previous=previous.first().ok_or_else(||Error::Integrity("budget state missing during settlement".into()))?;
                        let settled=add(decimal(previous.get::<_,&str>(0))?,actual)?;
                        self.execute("UPDATE pollard_budget_state SET settled=$4::text::numeric WHERE store_id=$1 AND scope_id=$2 AND meter=$3",&[store,&scope,&meter,&settled.to_string()])?;
                    },
                    "window" if actual!=Decimal::ZERO=>{self.execute("INSERT INTO pollard_window_events(store_id,scope_id,meter,amount,settled_at) VALUES($1,$2,$3,$4::text::numeric,$5)",&[store,&scope,&meter,&actual.to_string(),&now])?;},
                    "window"=>{},_=>return Err(Error::Integrity("invalid reservation kind".into()))
                }
            }
            self.execute("DELETE FROM pollard_reservations WHERE store_id=$1 AND reservation_id=$2",&[store,&id])?;
            self.execute("UPDATE pollard_reservation_state SET state='settled',charges_digest=$3,charges=$4,completed_at=$5 WHERE store_id=$1 AND reservation_id=$2",&[store,&id,&digest,&encoded,&now])?;
            Ok(())
        }))
    }
    pub fn release(&self, id: &str) -> Result<()> {
        self.reservation_retry(id,false,||self.transaction(true,||{
            let store=&self.options.store_id;
            let rows=self.query("SELECT state FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2 FOR UPDATE",&[store,&id])?;
            let Some(row)=rows.first()else{return Ok(());};
            let state:&str=row.get(0);if state=="released"{return Ok(());}
            if state!="active"{return Err(Error::Integrity(format!("reservation is already {state}: {id}")));}
            let now=self.now()?;
            self.execute("DELETE FROM pollard_reservations WHERE store_id=$1 AND reservation_id=$2",&[store,&id])?;
            self.execute("UPDATE pollard_reservation_state SET state='released',completed_at=$3 WHERE store_id=$1 AND reservation_id=$2",&[store,&id,&now])?;Ok(())
        }))
    }
    pub fn renew(&self, id: &str, lease: f64) -> Result<bool> {
        lease_valid(lease)?;
        self.retry(||self.transaction(true,||{
            let store=&self.options.store_id;
            let rows=self.query("SELECT state,expires_at FROM pollard_reservation_state WHERE store_id=$1 AND reservation_id=$2 FOR UPDATE",&[store,&id])?;
            let now=self.now()?;
            let Some(row)=rows.first()else{return Ok(false);};
            if row.get::<_,&str>(0)!="active"||row.get::<_,f64>(1)<=now{return Ok(false);}
            let expiry=now+lease;if !expiry.is_finite(){return Err(Error::Invalid("lease expiry overflow".into()));}
            self.execute("UPDATE pollard_reservation_state SET expires_at=$3 WHERE store_id=$1 AND reservation_id=$2",&[store,&id,&expiry])?;
            self.execute("UPDATE pollard_reservations SET expires_at=$3 WHERE store_id=$1 AND reservation_id=$2",&[store,&id,&expiry])?;Ok(true)
        }))
    }
}

struct TransactionGuard<'a> {
    store: &'a PostgresStore,
    active: bool,
}
impl Drop for TransactionGuard<'_> {
    fn drop(&mut self) {
        if self.active {
            let _ = self.store.batch("ROLLBACK");
            self.store.in_transaction.set(false);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn imported_numeric_amounts_reject_significant_precision_loss() {
        for input in [
            "1e-29",
            "0.00000000000000000000000000001",
            "1.23456789012345678901234567891",
        ] {
            assert!(decimal(input).is_err(), "{input}");
        }
        assert_eq!(decimal("1e-28").unwrap(), Decimal::new(1, 28));
        assert_eq!(
            decimal("1.00000000000000000000000000000").unwrap(),
            Decimal::ONE
        );
    }
    #[test]
    fn request_codec_sorts_scopes_and_preserves_decimal_scale() {
        let a = BudgetReservation {
            scope_id: "a".into(),
            limits: BTreeMap::from([("usd".into(), Decimal::new(30, 2))]),
            ..Default::default()
        };
        let b = BudgetReservation {
            scope_id: "b".into(),
            ..Default::default()
        };
        let (text, digest) = request_text(&[b.clone(), a.clone()], &[], 60.0).unwrap();
        assert_eq!(text,"{\"budgets\":[{\"baseline\":{},\"estimates\":{},\"limits\":{\"usd\":\"0.30\"},\"scope_id\":\"a\"},{\"baseline\":{},\"estimates\":{},\"limits\":{},\"scope_id\":\"b\"}],\"lease_seconds\":\"60.0\",\"windows\":[]}");
        assert_eq!(request_text(&[a, b], &[], 60.0).unwrap().1, digest);
        assert_eq!(
            charges_text(&BTreeMap::from([("usd".into(), Decimal::new(30, 2))]))
                .unwrap()
                .0,
            "{\"usd\":\"0.30\"}"
        );
    }
    #[test]
    fn invalid_leases_requests_and_stored_amounts_fail_closed() {
        for lease in [f64::NAN, f64::INFINITY, -1.0, 0.0] {
            assert!(request_text(&[], &[], lease).is_err());
        }
        let b = BudgetReservation {
            scope_id: "a".into(),
            limits: BTreeMap::from([("steps".into(), Decimal::ONE)]),
            ..Default::default()
        };
        assert!(request_text(&[b.clone(), b], &[], 1.0).is_err());
        for value in [
            "NaN",
            "Infinity",
            "-1",
            "1000000000000000000000000000000000000000000",
        ] {
            assert!(decimal(value).is_err());
        }
    }
    #[test]
    #[ignore = "requires isolated PostgreSQL: POLLARD_TEST_POSTGRES_DSN"]
    fn live_lost_commit_ack_retries_observe_permanent_tombstones() {
        let base = std::env::var("POLLARD_TEST_POSTGRES_DSN")
            .expect("POLLARD_TEST_POSTGRES_DSN is required");
        let schema = format!(
            "pollard_ack_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let mut admin = Client::connect(&base, NoTls).unwrap();
        admin
            .batch_execute(&format!("CREATE SCHEMA {schema}"))
            .unwrap();
        let dsn = format!("{base} options='-c search_path={schema}'");
        let store = PostgresStore::connect(dsn).unwrap();
        let b = BudgetReservation {
            scope_id: "r".into(),
            limits: BTreeMap::from([("steps".into(), Decimal::ONE)]),
            estimates: BTreeMap::from([("steps".into(), Decimal::ONE)]),
            ..Default::default()
        };
        let mut calls = 0;
        let accepted = store
            .reservation_retry("held", false, || {
                let result = store.reserve("held", std::slice::from_ref(&b), &[], 60.0)?;
                calls += 1;
                if calls == 1 {
                    Err(Error::Backend {
                        detail: "injected lost COMMIT acknowledgment".into(),
                        connection_lost: true,
                    })
                } else {
                    Ok(result)
                }
            })
            .unwrap();
        assert!(accepted.ok);
        assert_eq!(calls, 2);
        calls = 0;
        store
            .reservation_retry("held", true, || {
                store.settle("held", &BTreeMap::from([("steps".into(), Decimal::ONE)]))?;
                calls += 1;
                if calls == 1 {
                    Err(Error::Backend {
                        detail: "injected lost COMMIT acknowledgment".into(),
                        connection_lost: true,
                    })
                } else {
                    Ok(())
                }
            })
            .unwrap();
        assert_eq!(calls, 2);
        assert_eq!(
            store
                .query("SELECT settled::text FROM pollard_budget_state", &[])
                .unwrap()[0]
                .get::<_, &str>(0),
            "1"
        );
        assert_eq!(
            store
                .query("SELECT count(*) FROM pollard_reservation_state", &[])
                .unwrap()[0]
                .get::<_, i64>(0),
            1
        );
        drop(store);
        admin
            .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
            .unwrap();
    }
}
