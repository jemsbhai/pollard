//! Optional native Redis transport for the shared Python-compatible KV ledger.
//! WATCH/MULTI/EXEC verifies reads as well as writes. No write is sent before
//! the callback completes; network uncertainty is handled by retry tombstones.
//! Enable `redis-tls` for `rediss://` with OS certificate roots and hostname
//! validation. URL fragments cannot disable TLS verification.
use crate::kv::{KvBackend, KvTransaction, TransactionalKvStore};
use crate::{Error, Result};
use ::redis::{Connection, ErrorKind};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

pub type RedisConnector = Arc<dyn Fn() -> Result<Connection> + Send + Sync>;
#[derive(Debug, Clone)]
pub struct RedisOptions {
    pub store_id: String,
    pub prefix: String,
    pub create: bool,
    pub read_only: bool,
    pub watch_retries: usize,
    pub timeout: Duration,
}
impl Default for RedisOptions {
    fn default() -> Self {
        Self {
            store_id: "default".into(),
            prefix: "pollard".into(),
            create: true,
            read_only: false,
            watch_retries: 64,
            timeout: Duration::from_secs(10),
        }
    }
}
pub struct RedisBackend {
    connector: RedisConnector,
    connection: Mutex<Connection>,
    base: String,
    revision: String,
    options: RedisOptions,
    initialized: AtomicBool,
}
pub type RedisStore = TransactionalKvStore<RedisBackend>;
fn db(error: ::redis::RedisError) -> Error {
    let lost = error.is_io_error()
        || matches!(
            error.kind(),
            ErrorKind::BusyLoadingError
                | ErrorKind::ClusterDown
                | ErrorKind::MasterDown
                | ErrorKind::ReadOnly
        );
    Error::Backend {
        detail: format!("Redis: {error}"),
        connection_lost: lost,
    }
}
fn integrity(message: &str) -> Error {
    Error::Integrity(message.into())
}
impl RedisStore {
    pub fn open(url: &str) -> Result<Self> {
        Self::open_with_options(url, RedisOptions::default())
    }
    pub fn open_read_only(url: &str) -> Result<Self> {
        Self::open_with_options(
            url,
            RedisOptions {
                create: false,
                read_only: true,
                ..Default::default()
            },
        )
    }
    pub fn open_with_options(url: &str, options: RedisOptions) -> Result<Self> {
        let parsed =
            url::Url::parse(url).map_err(|_| Error::Invalid("invalid Redis URL".into()))?;
        if parsed.fragment().is_some() {
            return Err(Error::Invalid(
                "Redis URL fragments must not override TLS verification".into(),
            ));
        }
        if parsed.query_pairs().any(|(key, _)| {
            ["decode_responses", "encoding", "encoding_errors"].contains(&key.as_ref())
        }) {
            return Err(Error::Invalid(
                "Redis URL must not override Pollard text decoding options".into(),
            ));
        }
        let client = ::redis::Client::open(url).map_err(db)?;
        let timeout = options.timeout;
        Self::open_with_connector(
            Arc::new(move || {
                let c = client.get_connection_with_timeout(timeout).map_err(db)?;
                c.set_read_timeout(Some(timeout)).map_err(db)?;
                c.set_write_timeout(Some(timeout)).map_err(db)?;
                Ok(c)
            }),
            options,
        )
    }
    /// Caller-owned connection factory allows configured TLS/Unix transports.
    pub fn open_with_connector(connector: RedisConnector, options: RedisOptions) -> Result<Self> {
        if options.store_id.is_empty()
            || options.prefix.is_empty()
            || options.watch_retries == 0
            || options.timeout.is_zero()
        {
            return Err(Error::Invalid(
                "Redis store id, prefix, retries and timeout must be nonempty/positive".into(),
            ));
        }
        if options.read_only && options.create {
            return Err(Error::Invalid(
                "read-only Redis stores require create=false".into(),
            ));
        }
        let base = format!(
            "{{pollard-{}}}:{}",
            crate::identity::hash(&[], options.store_id.as_bytes()),
            options.prefix
        );
        let revision = format!("{base}:revision");
        let mut connection = connector()?;
        ::redis::cmd("PING")
            .query::<String>(&mut connection)
            .map_err(db)?;
        let backend = RedisBackend {
            connector,
            connection: Mutex::new(connection),
            base,
            revision,
            options,
            initialized: AtomicBool::new(false),
        };
        {
            let mut connection = backend
                .connection
                .lock()
                .map_err(|_| integrity("Redis connection lock poisoned"))?;
            backend.execute(&mut connection, backend.options.create, &mut |tx| {
                let identity = tx.get("schema", "redis-store-id")?;
                let version = tx.get("schema", "version")?;
                if tx.get("__internal_revision__", "")?.is_none() {
                    if !backend.options.create {
                        return Err(integrity("Redis store identity is missing"));
                    }
                    for bucket in ["schema", "nodes", "budget", "reservations", "window-events"] {
                        if !tx.items(bucket)?.is_empty() {
                            return Err(integrity("Redis store initialization state is partial"));
                        }
                    }
                    tx.put("schema", "redis-store-id", &backend.options.store_id)?;
                    tx.put("schema", "version", "1")?;
                    return Ok(());
                }
                if identity.as_deref() != Some(&backend.options.store_id)
                    || version.as_deref() != Some("1")
                {
                    return Err(integrity(
                        "Redis identity or schema version is missing or changed",
                    ));
                }
                Ok(())
            })?;
        }
        backend.initialized.store(true, Ordering::Release);
        let read_only = backend.options.read_only;
        Self::from_backend(backend, read_only)
    }
}
impl RedisBackend {
    pub fn base_key(&self) -> &str {
        &self.base
    }
    fn execute(
        &self,
        connection: &mut Connection,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()> {
        for _ in 0..self.options.watch_retries {
            ::redis::cmd("WATCH")
                .arg(&self.revision)
                .query::<()>(connection)
                .map_err(db)?;
            let outcome = (|| {
                let revision = ::redis::cmd("GET")
                    .arg(&self.revision)
                    .query::<Option<String>>(connection)
                    .map_err(db)?;
                match revision.as_deref() {
                    None if self.initialized.load(Ordering::Acquire) => {
                        return Err(integrity("Redis transaction revision is missing"))
                    }
                    Some(v)
                        if v.parse::<i64>()
                            .ok()
                            .filter(|n| *n > 0 && n.to_string() == v)
                            .is_none() =>
                    {
                        return Err(integrity("Redis transaction revision is invalid"))
                    }
                    _ => {}
                }
                let (seconds, micros) = ::redis::cmd("TIME")
                    .query::<(i64, i64)>(connection)
                    .map_err(db)?;
                if seconds < 0 || !(0..1_000_000).contains(&micros) {
                    return Err(integrity("Redis TIME returned an invalid value"));
                }
                let mut tx = RedisTransaction {
                    connection,
                    base: &self.base,
                    revision: revision.clone(),
                    now: seconds as f64 + micros as f64 / 1_000_000.0,
                    pending: BTreeMap::new(),
                };
                if self.initialized.load(Ordering::Acquire)
                    && (tx.get("schema", "redis-store-id")?.as_deref()
                        != Some(&self.options.store_id)
                        || tx.get("schema", "version")?.as_deref() != Some("1"))
                {
                    return Err(integrity(
                        "Redis identity or schema version is missing or changed",
                    ));
                }
                callback(&mut tx)?;
                if !writable && !tx.pending.is_empty() {
                    return Err(integrity("Redis read transaction attempted a write"));
                }
                if revision.is_none() && tx.pending.is_empty() {
                    return Err(integrity("Redis transaction revision is missing"));
                }
                if !tx.pending.is_empty() {
                    // Redis MULTI does not roll back runtime command errors.
                    // Check every known error before sending any mutation.
                    if revision.as_deref().and_then(|v| v.parse::<i64>().ok()) == Some(i64::MAX) {
                        return Err(integrity("Redis transaction revision is exhausted"));
                    }
                    let buckets = tx
                        .pending
                        .keys()
                        .map(|(bucket, _)| bucket)
                        .collect::<BTreeSet<_>>();
                    for bucket in buckets {
                        let kind = ::redis::cmd("TYPE")
                            .arg(format!("{}:bucket:{bucket}", self.base))
                            .query::<String>(tx.connection)
                            .map_err(db)?;
                        if kind != "none" && kind != "hash" {
                            return Err(integrity("Redis transaction bucket has invalid type"));
                        }
                    }
                }
                let mut pipe = ::redis::pipe();
                pipe.atomic();
                for ((bucket, key), value) in &tx.pending {
                    let bucket = format!("{}:bucket:{bucket}", self.base);
                    match value {
                        Some(value) => {
                            pipe.cmd("HSET").arg(bucket).arg(key).arg(value);
                        }
                        None => {
                            pipe.cmd("HDEL").arg(bucket).arg(key);
                        }
                    }
                }
                if tx.pending.is_empty() {
                    pipe.cmd("GET").arg(&self.revision);
                } else {
                    pipe.cmd("INCR").arg(&self.revision);
                }
                let committed = pipe
                    .query::<Option<Vec<::redis::Value>>>(tx.connection)
                    .map_err(db)?;
                Ok(committed.is_some())
            })();
            match outcome {
                Ok(true) => return Ok(()),
                Ok(false) => continue,
                Err(error) => {
                    let _ = ::redis::cmd("UNWATCH").query::<()>(connection);
                    return Err(error);
                }
            }
        }
        Err(Error::Backend {
            detail: format!(
                "Redis transaction exceeded {} WATCH conflicts",
                self.options.watch_retries
            ),
            connection_lost: false,
        })
    }
}
impl KvBackend for RedisBackend {
    fn transact(
        &self,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()> {
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| integrity("Redis connection lock poisoned"))?;
        self.execute(&mut connection, writable, callback)
    }
    fn reconnect(&self) -> Result<()> {
        let mut replacement = (self.connector)()?;
        ::redis::cmd("PING")
            .query::<String>(&mut replacement)
            .map_err(db)?;
        self.execute(&mut replacement, false, &mut |_| Ok(()))?;
        *self
            .connection
            .lock()
            .map_err(|_| integrity("Redis connection lock poisoned"))? = replacement;
        Ok(())
    }
}
struct RedisTransaction<'a> {
    connection: &'a mut Connection,
    base: &'a str,
    revision: Option<String>,
    now: f64,
    pending: BTreeMap<(String, String), Option<String>>,
}
impl KvTransaction for RedisTransaction<'_> {
    fn get(&mut self, bucket: &str, key: &str) -> Result<Option<String>> {
        if bucket == "__internal_revision__" {
            return Ok(self.revision.clone());
        }
        if let Some(value) = self.pending.get(&(bucket.into(), key.into())) {
            return Ok(value.clone());
        }
        ::redis::cmd("HGET")
            .arg(format!("{}:bucket:{bucket}", self.base))
            .arg(key)
            .query(self.connection)
            .map_err(db)
    }
    fn items(&mut self, bucket: &str) -> Result<Vec<(String, String)>> {
        let mut values = ::redis::cmd("HGETALL")
            .arg(format!("{}:bucket:{bucket}", self.base))
            .query::<BTreeMap<String, String>>(self.connection)
            .map_err(db)?;
        for ((b, k), value) in &self.pending {
            if b == bucket {
                match value {
                    Some(v) => {
                        values.insert(k.clone(), v.clone());
                    }
                    None => {
                        values.remove(k);
                    }
                }
            }
        }
        Ok(values.into_iter().collect())
    }
    fn put(&mut self, bucket: &str, key: &str, value: &str) -> Result<()> {
        self.pending
            .insert((bucket.into(), key.into()), Some(value.into()));
        Ok(())
    }
    fn delete(&mut self, bucket: &str, key: &str) -> Result<()> {
        self.pending.insert((bucket.into(), key.into()), None);
        Ok(())
    }
    fn now(&self) -> f64 {
        self.now
    }
}
