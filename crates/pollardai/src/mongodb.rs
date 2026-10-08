//! Native MongoDB replica-set/sharded storage; Python Pollard 1.6 schema compatible.
use crate::kv::{KvBackend, KvTransaction, TransactionalKvStore};
use crate::remote_worker::Worker;
use crate::{canonical_bytes, json, Error, Result};
use mongodb::{
    bson::{doc, Bson, Document},
    options::{
        Acknowledgment, FindOneAndUpdateOptions, FindOptions, IndexOptions, ReadConcern,
        ReadPreference, ReturnDocument, SelectionCriteria, TransactionOptions, WriteConcern,
    },
    sync::{Client, ClientSession, Collection},
    IndexModel,
};
use sha2::{Digest, Sha256};
use std::sync::Mutex;

#[derive(Clone, Debug)]
pub struct MongoOptions {
    pub database: String,
    pub store_id: String,
    pub collection_prefix: String,
    pub create: bool,
    pub read_only: bool,
}
impl Default for MongoOptions {
    fn default() -> Self {
        Self {
            database: "pollard".into(),
            store_id: "default".into(),
            collection_prefix: "pollard".into(),
            create: true,
            read_only: false,
        }
    }
}
pub type MongoStore = TransactionalKvStore<MongoBackend>;
pub struct MongoBackend {
    uri: String,
    options: MongoOptions,
    worker: Mutex<Worker<Connection>>,
}
impl TransactionalKvStore<MongoBackend> {
    pub fn connect(uri: &str) -> Result<Self> {
        Self::connect_with_options(uri, MongoOptions::default())
    }
    pub fn connect_with_options(uri: &str, options: MongoOptions) -> Result<Self> {
        validate_options(uri, &options)?;
        let worker = make_worker(uri, &options, options.create && !options.read_only)?;
        let read_only = options.read_only;
        Self::from_backend(
            MongoBackend {
                uri: uri.into(),
                options,
                worker: Mutex::new(worker),
            },
            read_only,
        )
    }
}
fn validate_options(uri: &str, o: &MongoOptions) -> Result<()> {
    if uri.is_empty()
        || o.database.is_empty()
        || o.database.contains('\0')
        || o.store_id.is_empty()
        || !o
            .collection_prefix
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphabetic)
        || !o
            .collection_prefix
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
    {
        return Err(Error::Invalid(
            "invalid MongoDB URI, database, store ID, or collection prefix".into(),
        ));
    }
    Ok(())
}
fn make_worker(uri: &str, options: &MongoOptions, create: bool) -> Result<Worker<Connection>> {
    let uri = uri.to_owned();
    let options = options.clone();
    Worker::new(move || Connection::open(&uri, options, create))
}
fn integrity(message: &str) -> Error {
    Error::Integrity(message.into())
}
fn mongo_error(e: mongodb::error::Error) -> Error {
    use mongodb::error::ErrorKind;
    let transient = e.contains_label("TransientTransactionError");
    let connection_lost = e.contains_label("UnknownTransactionCommitResult")
        || matches!(
            *e.kind,
            ErrorKind::Io(_)
                | ErrorKind::ConnectionPoolCleared { .. }
                | ErrorKind::ServerSelection { .. }
                | ErrorKind::DnsResolve { .. }
                | ErrorKind::Shutdown
        );
    Error::Backend {
        detail: format!(
            "MongoDB {}{e}",
            if transient {
                "transient transaction: "
            } else {
                ""
            }
        ),
        connection_lost,
    }
}
fn transient(e: &Error) -> bool {
    matches!(e, Error::Backend {detail, ..} if detail.starts_with("MongoDB transient transaction: "))
}
fn record_id(store: &str, bucket: &str, key: &str) -> Result<String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(canonical_bytes(&json!([store, bucket, key]))?)
    ))
}
fn validate_record(record: &Document, store: &str, bucket: &str, key: &str) -> Result<String> {
    let id = record_id(store, bucket, key)?;
    if record.get_str("_id").ok() != Some(id.as_str())
        || record.get_str("store_id").ok() != Some(store)
        || record.get_str("bucket").ok() != Some(bucket)
        || record.get_str("key").ok() != Some(key)
    {
        return Err(integrity("MongoDB Pollard record collision or corruption"));
    }
    record
        .get_str("value")
        .map(String::from)
        .map_err(|_| integrity("MongoDB Pollard record value must be a string"))
}
fn validate_coordinator(record: Option<&Document>, store: &str) -> Result<(i64, f64)> {
    let r = record.ok_or_else(|| integrity("MongoDB coordinator missing"))?;
    let revision = match r.get("revision") {
        Some(Bson::Int32(v)) => i64::from(*v),
        Some(Bson::Int64(v)) => *v,
        _ => 0,
    };
    let time = r
        .get_datetime("locked_at")
        .map_err(|_| integrity("MongoDB coordinator has no server time"))?
        .timestamp_millis();
    if r.get_str("_id").ok() != Some(store)
        || r.get_str("store_id").ok() != Some(store)
        || revision < 1
        || time < 0
    {
        return Err(integrity("MongoDB coordinator collision or corruption"));
    }
    Ok((revision, time as f64 / 1000.0))
}
struct Connection {
    client: Client,
    records: Collection<Document>,
    coordinators: Collection<Document>,
    session: Option<ClientSession>,
    store: String,
}
impl Connection {
    fn open(uri: &str, options: MongoOptions, create: bool) -> Result<Self> {
        let client = Client::with_uri_str(uri).map_err(mongo_error)?;
        let hello = client
            .database("admin")
            .run_command(doc! {"hello":1}, None)
            .map_err(mongo_error)?;
        if !hello.contains_key("setName") && hello.get_str("msg").ok() != Some("isdbgrid") {
            return Err(Error::Invalid(
                "MongoDB requires a replica set or sharded deployment".into(),
            ));
        }
        let database = client.database(&options.database);
        let mut c = Self {
            client,
            records: database.collection(&format!("{}_records", options.collection_prefix)),
            coordinators: database
                .collection(&format!("{}_coordinators", options.collection_prefix)),
            store: options.store_id,
            session: None,
        };
        c.begin(false)?;
        let state = c.namespace();
        c.abort();
        let fresh = state?;
        if fresh && !create {
            return Err(integrity("MongoDB namespace is missing"));
        }
        c.indexes(fresh && create)?;
        if fresh {
            c.retry_initialize()?;
            c.indexes(false)?;
        }
        Ok(c)
    }
    fn begin(&mut self, write: bool) -> Result<f64> {
        let mut session = self.client.start_session(None).map_err(mongo_error)?;
        session
            .start_transaction(
                TransactionOptions::builder()
                    .read_concern(ReadConcern::snapshot())
                    .write_concern(WriteConcern::builder().w(Acknowledgment::Majority).build())
                    .selection_criteria(SelectionCriteria::ReadPreference(ReadPreference::Primary))
                    .build(),
            )
            .map_err(mongo_error)?;
        self.session = Some(session);
        if write {
            let s = self.session.as_mut().expect("active transaction");
            let current = self
                .coordinators
                .find_one_with_session(doc! {"_id":&self.store}, None, s)
                .map_err(mongo_error)?;
            let (revision, _) = validate_coordinator(current.as_ref(), &self.store)?;
            let update =
                vec![doc! {"$set":{"revision":{"$add":["$revision",1]},"locked_at":"$$NOW"}}];
            let record = self
                .coordinators
                .find_one_and_update_with_session(
                    doc! {"_id":&self.store,"store_id":&self.store,"revision":revision},
                    update,
                    FindOneAndUpdateOptions::builder()
                        .return_document(ReturnDocument::After)
                        .build(),
                    s,
                )
                .map_err(mongo_error)?;
            Ok(validate_coordinator(record.as_ref(), &self.store)?.1)
        } else {
            // The admin clock is server generated. No data is written by read transactions.
            let hello = self
                .client
                .database("admin")
                .run_command(doc! {"hello":1}, None)
                .map_err(mongo_error)?;
            let time = hello
                .get_datetime("localTime")
                .map_err(|_| integrity("MongoDB server clock missing"))?
                .timestamp_millis();
            if time < 0 {
                return Err(integrity("MongoDB invalid server clock"));
            }
            Ok(time as f64 / 1000.0)
        }
    }
    fn abort(&mut self) {
        if let Some(mut s) = self.session.take() {
            let _ = s.abort_transaction();
        }
    }
    fn commit(&mut self) -> Result<()> {
        let mut s = self
            .session
            .take()
            .ok_or_else(|| integrity("MongoDB transaction missing"))?;
        // A lost commit acknowledgement may be retried on the same transaction ID.
        for attempt in 0..16 {
            match s.commit_transaction() {
                Err(e) if e.contains_label("UnknownTransactionCommitResult") && attempt < 15 => {
                    continue
                }
                other => return other.map_err(mongo_error),
            }
        }
        unreachable!()
    }
    fn namespace(&mut self) -> Result<bool> {
        let version = self.get("schema", "version")?;
        let s = self.session.as_mut().expect("active transaction");
        let coordinator = self
            .coordinators
            .find_one_with_session(doc! {"_id":&self.store}, None, s)
            .map_err(mongo_error)?;
        if version.is_none() {
            let record = self
                .records
                .find_one_with_session(doc! {"store_id":&self.store}, None, s)
                .map_err(mongo_error)?;
            if record.is_none() && coordinator.is_none() {
                return Ok(true);
            }
            return Err(integrity("MongoDB partial namespace initialization"));
        }
        if version.as_deref() != Some("1") {
            return Err(integrity("MongoDB unsupported schema version"));
        }
        validate_coordinator(coordinator.as_ref(), &self.store)?;
        Ok(false)
    }
    fn indexes(&self, create: bool) -> Result<()> {
        let indexes = match self.records.list_indexes(None) {
            Ok(cursor) => cursor
                .collect::<mongodb::error::Result<Vec<_>>>()
                .map_err(mongo_error)?,
            Err(e) if matches!(e.kind.as_ref(),mongodb::error::ErrorKind::Command(c) if c.code==26) => {
                Vec::new()
            }
            Err(e) => return Err(mongo_error(e)),
        };
        let expected = doc! {"store_id":1,"bucket":1,"key":1};
        let mut matches = Vec::new();
        for index in indexes {
            let ordered = index.keys.iter().eq(expected.iter());
            let options = index.options.unwrap_or_default();
            if ordered {
                matches.push(options);
            } else if index.keys != doc! {"_id":1} && options.unique == Some(true) {
                return Err(integrity("MongoDB incompatible unique index"));
            }
        }
        if matches.is_empty() && create {
            self.records
                .create_index(
                    IndexModel::builder()
                        .keys(expected)
                        .options(
                            IndexOptions::builder()
                                .unique(true)
                                .name("pollard_store_bucket_key_unique".to_owned())
                                .build(),
                        )
                        .build(),
                    None,
                )
                .map_err(mongo_error)?;
            return self.indexes(false);
        }
        if matches.len() != 1 {
            return Err(integrity(
                "MongoDB unique record index missing or duplicated",
            ));
        }
        let index = &matches[0];
        if index.unique != Some(true)
            || index.sparse == Some(true)
            || index.partial_filter_expression.is_some()
            || index
                .collation
                .as_ref()
                .is_some_and(|c| c.locale != "simple")
        {
            return Err(integrity("MongoDB unique record index incompatible"));
        }
        Ok(())
    }
    fn retry_initialize(&mut self) -> Result<()> {
        for attempt in 0..16 {
            let result = (|| {
                self.begin(false)?;
                if self.namespace()? {
                    let s = self.session.as_mut().expect("active transaction");
                    let r=self.coordinators.find_one_and_update_with_session(doc! {"_id":&self.store}, vec![doc! {"$set":{"store_id":&self.store,"revision":1,"locked_at":"$$NOW"}}],FindOneAndUpdateOptions::builder().upsert(true).return_document(ReturnDocument::After).build(),s).map_err(mongo_error)?;
                    validate_coordinator(r.as_ref(), &self.store)?;
                    self.put("schema", "version", "1")?;
                }
                self.commit()
            })();
            self.abort();
            match result {
                Err(e) if transient(&e) && attempt < 15 => {
                    std::thread::sleep(std::time::Duration::from_millis(5))
                }
                other => return other,
            }
        }
        unreachable!()
    }
    fn get(&mut self, bucket: &str, key: &str) -> Result<Option<String>> {
        let id = record_id(&self.store, bucket, key)?;
        self.records
            .find_one_with_session(
                doc! {"_id":id},
                None,
                self.session.as_mut().expect("active transaction"),
            )
            .map_err(mongo_error)?
            .map(|r| validate_record(&r, &self.store, bucket, key))
            .transpose()
    }
    fn items(&mut self, bucket: &str) -> Result<Vec<(String, String)>> {
        let s = self.session.as_mut().expect("active transaction");
        let mut cursor = self
            .records
            .find_with_session(
                doc! {"store_id":&self.store,"bucket":bucket},
                FindOptions::builder().sort(doc! {"key":1}).build(),
                s,
            )
            .map_err(mongo_error)?;
        cursor
            .iter(s)
            .map(|record| {
                let r = record.map_err(mongo_error)?;
                let key = r
                    .get_str("key")
                    .map_err(|_| integrity("MongoDB record key invalid"))?;
                Ok((key.into(), validate_record(&r, &self.store, bucket, key)?))
            })
            .collect()
    }
    fn put(&mut self, bucket: &str, key: &str, value: &str) -> Result<()> {
        self.get(bucket, key)?;
        let id = record_id(&self.store, bucket, key)?;
        self.records
            .replace_one_with_session(
                doc! {"_id":&id},
                doc! {"_id":id,"store_id":&self.store,"bucket":bucket,"key":key,"value":value},
                mongodb::options::ReplaceOptions::builder()
                    .upsert(true)
                    .build(),
                self.session.as_mut().expect("active transaction"),
            )
            .map_err(mongo_error)?;
        Ok(())
    }
    fn delete(&mut self, bucket: &str, key: &str) -> Result<()> {
        self.get(bucket, key)?;
        self.records
            .delete_one_with_session(
                doc! {"_id":record_id(&self.store,bucket,key)?},
                None,
                self.session.as_mut().expect("active transaction"),
            )
            .map_err(mongo_error)?;
        Ok(())
    }
}
struct Transaction<'a> {
    worker: &'a Worker<Connection>,
    time: f64,
    writable: bool,
}
impl Drop for Transaction<'_> {
    fn drop(&mut self) {
        let _ = self.worker.call(|c| {
            c.abort();
            Ok(())
        });
    }
}
impl KvTransaction for Transaction<'_> {
    fn get(&mut self, bucket: &str, key: &str) -> Result<Option<String>> {
        let (b, k) = (bucket.to_owned(), key.to_owned());
        self.worker.call(move |c| c.get(&b, &k))
    }
    fn items(&mut self, bucket: &str) -> Result<Vec<(String, String)>> {
        let b = bucket.to_owned();
        self.worker.call(move |c| c.items(&b))
    }
    fn put(&mut self, bucket: &str, key: &str, value: &str) -> Result<()> {
        if !self.writable {
            return Err(Error::Invalid("read-only transaction".into()));
        }
        let (b, k, v) = (bucket.to_owned(), key.to_owned(), value.to_owned());
        self.worker.call(move |c| c.put(&b, &k, &v))
    }
    fn delete(&mut self, bucket: &str, key: &str) -> Result<()> {
        if !self.writable {
            return Err(Error::Invalid("read-only transaction".into()));
        }
        let (b, k) = (bucket.to_owned(), key.to_owned());
        self.worker.call(move |c| c.delete(&b, &k))
    }
    fn now(&self) -> f64 {
        self.time
    }
}
impl KvBackend for MongoBackend {
    fn transact(
        &self,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()> {
        if writable && self.options.read_only {
            return Err(Error::Invalid("store is read-only".into()));
        }
        let worker = self
            .worker
            .lock()
            .map_err(|_| integrity("MongoDB worker poisoned"))?;
        for attempt in 0..16 {
            let result = (|| {
                let now = worker.call(move |c| c.begin(writable))?;
                let mut tx = Transaction {
                    worker: &worker,
                    time: now,
                    writable,
                };
                callback(&mut tx)?;
                worker.call(Connection::commit)
            })();
            // begin can fail after opening a transaction, before the RAII guard exists.
            let _ = worker.call(|c| {
                c.abort();
                Ok(())
            });
            match result {
                Err(e) if transient(&e) && attempt < 15 => {
                    std::thread::sleep(std::time::Duration::from_millis(5))
                }
                other => return other,
            }
        }
        unreachable!()
    }
    fn reconnect(&self) -> Result<()> {
        let replacement = make_worker(&self.uri, &self.options, false)?;
        *self
            .worker
            .lock()
            .map_err(|_| integrity("MongoDB worker poisoned"))? = replacement;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_configuration_fails_before_connecting() {
        for prefix in ["", "1bad", "unsafe-name", "a.b", "é"] {
            let options = MongoOptions {
                collection_prefix: prefix.into(),
                ..Default::default()
            };
            assert!(matches!(
                MongoStore::connect_with_options("mongodb://127.0.0.1:9", options),
                Err(Error::Invalid(_))
            ));
        }
    }
    #[test]
    fn bson_coordinator_identity_and_types_are_strict() {
        let valid = doc! {"_id":"s","store_id":"s","revision":1,"locked_at":mongodb::bson::DateTime::from_millis(1000)};
        assert_eq!(validate_coordinator(Some(&valid), "s").unwrap(), (1, 1.0));
        for revision in [
            Bson::Boolean(true),
            Bson::Double(1.0),
            Bson::Int64(0),
            Bson::String("1".into()),
        ] {
            let mut bad = valid.clone();
            bad.insert("revision", revision);
            assert!(validate_coordinator(Some(&bad), "s").is_err());
        }
        assert!(validate_coordinator(Some(&valid), "other").is_err());
        let mut bad = valid;
        bad.insert(
            "locked_at",
            Bson::DateTime(mongodb::bson::DateTime::from_millis(-1)),
        );
        assert!(validate_coordinator(Some(&bad), "s").is_err());
    }
}
