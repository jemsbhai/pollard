//! Native routed Neo4j storage with Python-compatible graph schema and ledger.
//! Reads and writes use writer routing and the driver's shared bookmark manager.
use crate::kv::{KvBackend, KvTransaction, TransactionalKvStore};
use crate::{canonical_bytes, json, Error, Result, Value};
use neo4j_driver::{
    driver::{auth::AuthToken, ConnectionConfig, Driver, DriverConfig, RoutingControl},
    session::SessionConfig,
    ValueReceive, ValueSend,
};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
const CONSTRAINTS: [(&str, &str, &str); 2] = [
    ("pollard_neo4j_kv_record_key", "_PollardKV", "record_key"),
    (
        "pollard_neo4j_coordinator_key",
        "_PollardCoordinator",
        "coordinator_key",
    ),
];
#[derive(Clone, Debug)]
pub struct Neo4jOptions {
    pub database: String,
    pub store_id: String,
    pub create: bool,
    pub read_only: bool,
}
impl Default for Neo4jOptions {
    fn default() -> Self {
        Self {
            database: "neo4j".into(),
            store_id: "default".into(),
            create: true,
            read_only: false,
        }
    }
}
pub type Neo4jStore = TransactionalKvStore<Neo4jBackend>;
pub struct Neo4jBackend {
    uri: String,
    user: String,
    password: String,
    options: Neo4jOptions,
    driver: Mutex<Arc<Driver>>,
}
impl TransactionalKvStore<Neo4jBackend> {
    pub fn connect(uri: &str, user: &str, password: &str) -> Result<Self> {
        Self::connect_with_options(uri, user, password, Neo4jOptions::default())
    }
    pub fn connect_with_options(
        uri: &str,
        user: &str,
        password: &str,
        options: Neo4jOptions,
    ) -> Result<Self> {
        if uri.is_empty()
            || user.is_empty()
            || options.database.is_empty()
            || options.store_id.is_empty()
        {
            return Err(Error::Invalid(
                "Neo4j URI, user, database and store ID must be nonempty".into(),
            ));
        }
        let backend = Neo4jBackend {
            uri: uri.into(),
            user: user.into(),
            password: password.into(),
            options: options.clone(),
            driver: Mutex::new(Arc::new(connect_driver(uri, user, password)?)),
        };
        backend.initialize(options.create && !options.read_only)?;
        Self::from_backend(backend, options.read_only)
    }
}
fn connect_driver(uri: &str, user: &str, password: &str) -> Result<Driver> {
    let connection = ConnectionConfig::try_from(uri)
        .map_err(|e| Error::Invalid(format!("invalid Neo4j connection configuration: {e}")))?;
    let driver = Driver::new(
        connection,
        DriverConfig::new().with_auth(Arc::new(AuthToken::new_basic_auth(user, password))),
    );
    driver.verify_connectivity().map_err(neo_error)?;
    Ok(driver)
}
fn integrity(s: &str) -> Error {
    Error::Integrity(s.into())
}
fn neo_error(e: neo4j_driver::Neo4jError) -> Error {
    use neo4j_driver::Neo4jError;
    let retry = e.is_retryable();
    let connection_lost = matches!(
        &e,
        Neo4jError::Disconnect { .. } | Neo4jError::Timeout { .. }
    );
    Error::Backend {
        detail: format!(
            "Neo4j {}{e}",
            if retry && !connection_lost {
                "transient transaction: "
            } else {
                ""
            }
        ),
        connection_lost,
    }
}
fn transient(e: &Error) -> bool {
    matches!(e,Error::Backend{detail,..}if detail.starts_with("Neo4j transient transaction: "))
}
fn receive(v: ValueReceive) -> Result<Value> {
    Ok(match v {
        ValueReceive::Null => Value::Null,
        ValueReceive::Boolean(v) => json!(v),
        ValueReceive::Integer(v) => json!(v),
        ValueReceive::String(v) => json!(v),
        ValueReceive::List(v) => Value::Array(v.into_iter().map(receive).collect::<Result<_>>()?),
        ValueReceive::Map(v) => Value::Object(
            v.into_iter()
                .map(|(k, v)| Ok((k, receive(v)?)))
                .collect::<Result<_>>()?,
        ),
        _ => return Err(integrity("Neo4j invalid Pollard record property type")),
    })
}
struct Query {
    text: String,
    parameters: HashMap<String, ValueSend>,
}
fn query(text: &str) -> Query {
    Query {
        text: text.into(),
        parameters: HashMap::new(),
    }
}
impl Query {
    fn param(mut self, name: &str, value: impl Into<String>) -> Self {
        self.parameters
            .insert(name.into(), ValueSend::String(value.into()));
        self
    }
}
fn digest(v: Value) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(canonical_bytes(&v)?)))
}
fn record_key(store: &str, bucket: &str, key: &str) -> Result<String> {
    digest(json!(["neo4j-record", store, bucket, key]))
}
fn coordinator_key(store: &str) -> Result<String> {
    digest(json!(["neo4j-coordinator", store]))
}
fn validate_record(r: &Value, store: &str, bucket: &str, key: &str) -> Result<String> {
    if r.get("record_key").and_then(Value::as_str) != Some(record_key(store, bucket, key)?.as_str())
        || r.get("store_id").and_then(Value::as_str) != Some(store)
        || r.get("bucket").and_then(Value::as_str) != Some(bucket)
        || r.get("item_key").and_then(Value::as_str) != Some(key)
    {
        return Err(integrity("Neo4j record key collision or corruption"));
    }
    r.get("value")
        .and_then(Value::as_str)
        .map(String::from)
        .ok_or_else(|| integrity("Neo4j record value must be a string"))
}
fn validate_coordinator(r: Option<&Value>, store: &str) -> Result<()> {
    let r = r.ok_or_else(|| integrity("Neo4j coordinator missing"))?;
    if r.get("coordinator_key").and_then(Value::as_str) != Some(coordinator_key(store)?.as_str())
        || r.get("store_id").and_then(Value::as_str) != Some(store)
        || r.get("revision").and_then(Value::as_i64).unwrap_or(0) < 1
    {
        return Err(integrity("Neo4j coordinator collision or corruption"));
    }
    Ok(())
}

struct Connection<'a> {
    run: &'a mut dyn FnMut(Query) -> Result<Vec<Value>>,
    store: String,
    time: f64,
    writable: bool,
}
impl Connection<'_> {
    fn rows(&mut self, q: Query) -> Result<Vec<Value>> {
        (self.run)(q)
    }
    fn clock(&mut self) -> Result<f64> {
        let c = self.one(query(
            "RETURN {epoch_millis:datetime.realtime().epochMillis} AS properties",
        ))?;
        let t = c
            .as_ref()
            .and_then(|r| r["epoch_millis"].as_i64())
            .filter(|t| *t >= 0)
            .ok_or_else(|| integrity("Neo4j invalid server clock"))?;
        Ok(t as f64 / 1000.0)
    }
    fn one(&mut self, q: Query) -> Result<Option<Value>> {
        let mut rows = self.rows(q)?;
        if rows.len() > 1 {
            return Err(integrity("Neo4j duplicate record identity"));
        }
        Ok(rows.pop())
    }
    fn coordinator(&mut self) -> Result<Option<Value>> {
        self.one(query("MATCH (c:_PollardCoordinator {coordinator_key:$key}) RETURN properties(c) AS properties").param("key",coordinator_key(&self.store)?))
    }
    fn lock(&mut self) -> Result<()> {
        let c = self.coordinator()?;
        validate_coordinator(c.as_ref(), &self.store)?;
        let c=self.one(query("MATCH (c:_PollardCoordinator {coordinator_key:$key,store_id:$store}) SET c.revision=c.revision+1 RETURN properties(c) AS properties").param("key",coordinator_key(&self.store)?).param("store",self.store.clone()))?;
        validate_coordinator(c.as_ref(), &self.store)
    }
    fn namespace(&mut self) -> Result<bool> {
        let version = self.get("schema", "version")?;
        let coordinator = self.coordinator()?;
        if version.is_none() {
            let records=self.rows(query("MATCH (r:_PollardKV {store_id:$store}) RETURN properties(r) AS properties LIMIT 1").param("store",self.store.clone()))?;
            if records.is_empty() && coordinator.is_none() {
                return Ok(true);
            }
            return Err(integrity("Neo4j partial namespace initialization"));
        }
        if version.as_deref() != Some("1") {
            return Err(integrity("Neo4j unsupported schema version"));
        }
        validate_coordinator(coordinator.as_ref(), &self.store)?;
        Ok(false)
    }
    fn constraint_state(&mut self) -> Result<bool> {
        let constraints=self.rows(query("SHOW CONSTRAINTS YIELD name,type,entityType,labelsOrTypes,properties,ownedIndex RETURN {name:name,type:type,entityType:entityType,labelsOrTypes:labelsOrTypes,properties:properties,ownedIndex:ownedIndex} AS properties"))?;
        let indexes=self.rows(query("SHOW INDEXES YIELD name,state,type,entityType,labelsOrTypes,properties,owningConstraint RETURN {name:name,state:state,type:type,entityType:entityType,labelsOrTypes:labelsOrTypes,properties:properties,owningConstraint:owningConstraint} AS properties"))?;
        let any_named = CONSTRAINTS.iter().any(|(name, _, _)| {
            constraints
                .iter()
                .chain(&indexes)
                .any(|r| r["name"] == *name)
        });
        if !any_named {
            let collision = CONSTRAINTS.iter().any(|(_, label, prop)| {
                constraints.iter().chain(&indexes).any(|r| {
                    r["entityType"] == "NODE"
                        && r["labelsOrTypes"] == json!([label])
                        && r["properties"] == json!([prop])
                        && matches!(
                            r["type"].as_str(),
                            Some("UNIQUENESS" | "NODE_PROPERTY_UNIQUENESS" | "RANGE")
                        )
                })
            });
            if collision {
                return Err(integrity("Neo4j incompatible unnamed constraints/indexes"));
            }
            return Ok(true);
        }
        for (name, label, prop) in CONSTRAINTS {
            let c = constraints
                .iter()
                .find(|r| r["name"] == name)
                .ok_or_else(|| integrity("Neo4j constraint missing"))?;
            let i = indexes
                .iter()
                .find(|r| r["name"] == name)
                .ok_or_else(|| integrity("Neo4j constraint index missing"))?;
            if !matches!(
                c["type"].as_str(),
                Some("UNIQUENESS" | "NODE_PROPERTY_UNIQUENESS")
            ) || c["ownedIndex"] != name
                || i["owningConstraint"] != name
                || i["state"] != "ONLINE"
                || i["type"] != "RANGE"
                || [c, i].iter().any(|r| {
                    r["entityType"] != "NODE"
                        || r["labelsOrTypes"] != json!([label])
                        || r["properties"] != json!([prop])
                })
            {
                return Err(integrity("Neo4j constraint/index incompatible"));
            }
        }
        Ok(false)
    }
    fn get(&mut self, bucket: &str, key: &str) -> Result<Option<String>> {
        self.one(
            query("MATCH (r:_PollardKV {record_key:$key}) RETURN properties(r) AS properties")
                .param("key", record_key(&self.store, bucket, key)?),
        )?
        .map(|r| validate_record(&r, &self.store, bucket, key))
        .transpose()
    }
    fn items(&mut self, bucket: &str) -> Result<Vec<(String, String)>> {
        self.rows(query("MATCH (r:_PollardKV {store_id:$store,bucket:$bucket}) RETURN properties(r) AS properties ORDER BY r.item_key ASC").param("store",self.store.clone()).param("bucket",bucket))?.into_iter().map(|r|{let key=r["item_key"].as_str().ok_or_else(||integrity("Neo4j record key invalid"))?;Ok((key.into(),validate_record(&r,&self.store,bucket,key)?))}).collect()
    }
    fn put(&mut self, bucket: &str, key: &str, value: &str) -> Result<()> {
        let id = record_key(&self.store, bucket, key)?;
        let r=self.one(query("MERGE (r:_PollardKV {record_key:$id}) ON CREATE SET r.store_id=$store,r.bucket=$bucket,r.item_key=$key,r.value=$value RETURN properties(r) AS properties").param("id",id.clone()).param("store",self.store.clone()).param("bucket",bucket).param("key",key).param("value",value))?.ok_or_else(||integrity("Neo4j record disappeared during put"))?;
        validate_record(&r, &self.store, bucket, key)?;
        let updated=self.one(query("MATCH (r:_PollardKV {record_key:$id,store_id:$store,bucket:$bucket,item_key:$key}) SET r.value=$value RETURN properties(r) AS properties").param("id",id).param("store",self.store.clone()).param("bucket",bucket).param("key",key).param("value",value))?.ok_or_else(||integrity("Neo4j record changed during put"))?;
        validate_record(&updated, &self.store, bucket, key)?;
        Ok(())
    }
    fn delete(&mut self, bucket: &str, key: &str) -> Result<()> {
        if self.get(bucket, key)?.is_none() {
            return Ok(());
        }
        let count=self.one(query("MATCH (r:_PollardKV {record_key:$id,store_id:$store,bucket:$bucket,item_key:$key}) DELETE r RETURN {deleted:count(*)} AS properties").param("id",record_key(&self.store,bucket,key)?).param("store",self.store.clone()).param("bucket",bucket).param("key",key))?;
        if count.as_ref().and_then(|r| r["deleted"].as_i64()) != Some(1) {
            return Err(integrity("Neo4j record changed during delete"));
        }
        Ok(())
    }
}
impl KvTransaction for Connection<'_> {
    fn get(&mut self, b: &str, k: &str) -> Result<Option<String>> {
        Connection::get(self, b, k)
    }
    fn items(&mut self, b: &str) -> Result<Vec<(String, String)>> {
        Connection::items(self, b)
    }
    fn put(&mut self, b: &str, k: &str, v: &str) -> Result<()> {
        if !self.writable {
            return Err(Error::Invalid("read-only transaction".into()));
        }
        Connection::put(self, b, k, v)
    }
    fn delete(&mut self, b: &str, k: &str) -> Result<()> {
        if !self.writable {
            return Err(Error::Invalid("read-only transaction".into()));
        }
        Connection::delete(self, b, k)
    }
    fn now(&self) -> f64 {
        self.time
    }
}
impl Neo4jBackend {
    // The driver's transaction API fixes its callback error type to Neo4jError.
    #[allow(clippy::result_large_err)]
    fn execute<T>(
        &self,
        lock: bool,
        writable: bool,
        mut callback: impl FnMut(&mut Connection<'_>) -> Result<T>,
    ) -> Result<T> {
        let driver = self
            .driver
            .lock()
            .map_err(|_| integrity("Neo4j driver poisoned"))?
            .clone();
        for attempt in 0..16 {
            let config = SessionConfig::new()
                .with_database(Arc::new(self.options.database.clone()))
                .with_bookmark_manager(driver.execute_query_bookmark_manager());
            let mut session = driver.session(config);
            let response = session
                .transaction()
                .with_routing_control(RoutingControl::Write)
                .run(|tx| {
                    let mut run = |q: Query| -> Result<Vec<Value>> {
                        let cursor = tx
                            .query(q.text)
                            .with_parameters(q.parameters)
                            .run()
                            .map_err(neo_error)?;
                        cursor
                            .map(|row| {
                                let row = row.map_err(neo_error)?;
                                receive(
                                    row.into_values()
                                        .next()
                                        .ok_or_else(|| integrity("Neo4j empty result record"))?,
                                )
                            })
                            .collect()
                    };
                    let result = {
                        let mut c = Connection {
                            run: &mut run,
                            store: self.options.store_id.clone(),
                            time: 0.0,
                            writable,
                        };
                        (|| {
                            if lock {
                                c.lock()?;
                            }
                            c.time = c.clock()?;
                            callback(&mut c)
                        })()
                    };
                    match result {
                        Ok(value) => {
                            tx.commit()?;
                            Ok(Ok(value))
                        }
                        Err(e) => Ok(Err(e)),
                    }
                })
                .map_err(neo_error)
                .and_then(|r| r);
            match response {
                Err(e) if transient(&e) && attempt < 15 => {
                    std::thread::sleep(std::time::Duration::from_millis(5))
                }
                other => return other,
            }
        }
        unreachable!()
    }
    fn initialize(&self, create: bool) -> Result<()> {
        let fresh = self.execute(false, false, |c| c.namespace())?;
        if fresh && !create {
            return Err(integrity("Neo4j namespace missing"));
        }
        let constraints_fresh = self.execute(false, false, |c| c.constraint_state())?;
        if constraints_fresh {
            if !fresh || !create {
                return Err(integrity("Neo4j constraints missing"));
            }
            self.execute(false,true,|c|{for(name,label,prop)in CONSTRAINTS{c.rows(query(&format!("CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")))?;}Ok(())})?;
            if self.execute(false, false, |c| c.constraint_state())? {
                return Err(integrity("Neo4j constraints were not created"));
            }
        }
        if fresh {
            self.execute(false,true,|c|{if c.namespace()?{let coordinator=c.one(query("MERGE (c:_PollardCoordinator {coordinator_key:$key}) ON CREATE SET c.store_id=$store,c.revision=1 RETURN properties(c) AS properties").param("key",coordinator_key(&c.store)?).param("store",c.store.clone()))?;validate_coordinator(coordinator.as_ref(),&c.store)?;c.put("schema","version","1")?;}Ok(())})?;
        }
        Ok(())
    }
}
impl KvBackend for Neo4jBackend {
    fn transact(
        &self,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()> {
        if writable && self.options.read_only {
            return Err(Error::Invalid("store is read-only".into()));
        }
        self.execute(writable, writable, |c| callback(c))
    }
    fn reconnect(&self) -> Result<()> {
        let replacement = Arc::new(connect_driver(&self.uri, &self.user, &self.password)?);
        let candidate = Self {
            uri: self.uri.clone(),
            user: self.user.clone(),
            password: self.password.clone(),
            options: self.options.clone(),
            driver: Mutex::new(replacement.clone()),
        };
        candidate.initialize(false)?;
        *self
            .driver
            .lock()
            .map_err(|_| integrity("Neo4j driver poisoned"))? = replacement;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_uri_is_rejected_without_network_fallback() {
        assert!(matches!(
            Neo4jStore::connect("https://example.invalid", "user", "pass"),
            Err(Error::Invalid(_))
        ));
    }
    #[test]
    fn constraints_require_exact_names_signatures_and_online_owned_indexes() {
        let constraints:Vec<_>=CONSTRAINTS.iter().map(|(n,l,p)|json!({"name":n,"type":"UNIQUENESS","entityType":"NODE","labelsOrTypes":[l],"properties":[p],"ownedIndex":n})).collect();
        let indexes:Vec<_>=CONSTRAINTS.iter().map(|(n,l,p)|json!({"name":n,"type":"RANGE","entityType":"NODE","labelsOrTypes":[l],"properties":[p],"owningConstraint":n,"state":"ONLINE"})).collect();
        for case in 0..5 {
            let mut cs = constraints.clone();
            let mut ix = indexes.clone();
            match case {
                1 => {
                    cs.pop();
                }
                2 => ix[0]["state"] = json!("POPULATING"),
                3 => cs[0]["ownedIndex"] = json!("wrong"),
                4 => cs[0]["properties"] = json!(["other"]),
                _ => {}
            }
            let mut run = |q: Query| {
                Ok(if q.text.starts_with("SHOW CONSTRAINTS") {
                    cs.clone()
                } else {
                    ix.clone()
                })
            };
            let mut c = Connection {
                run: &mut run,
                store: "s".into(),
                time: 1.0,
                writable: false,
            };
            if case == 0 {
                assert!(!c.constraint_state().unwrap());
            } else {
                assert!(c.constraint_state().is_err());
            }
        }
    }
}
