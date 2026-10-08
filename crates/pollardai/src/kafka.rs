//! Ordered Kafka audit log compatible with Pollard 1.6.0 KafkaStore.
//!
//! A dedicated topic must have exactly partition zero, delete-only cleanup and
//! unlimited byte/time retention. Commands have application-level idempotency.
//! Kafka orders commands; this store deliberately provides no budget arbiter.
use crate::{json, Error, MemoryStore, Node, RecordingStore, Result, Store, Value};
use rdkafka::{
    admin::{AdminClient, AdminOptions, ResourceSpecifier},
    client::{ClientContext, DefaultClientContext},
    config::ClientConfig,
    consumer::{BaseConsumer, Consumer},
    message::Message,
    producer::{BaseProducer, BaseRecord, DeliveryResult, ProducerContext},
    Offset, TopicPartitionList,
};
use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    sync::mpsc,
    time::{Duration, Instant},
};

const EVENT_FIELDS: [&str; 6] = [
    "version",
    "store_id",
    "operation_id",
    "operation",
    "body",
    "request_digest",
];
const NODE_FIELDS: [&str; 8] = [
    "id",
    "parent",
    "kind",
    "attempt",
    "payload",
    "result",
    "result_digest",
    "meta",
];
const CONTROLLED: [&str; 10] = [
    "acks",
    "enable.auto.commit",
    "enable.auto.offset.store",
    "enable.idempotence",
    "enable.partition.eof",
    "group.id",
    "isolation.level",
    "auto.offset.reset",
    "allow.auto.create.topics",
    "group.instance.id",
];
fn invalid(detail: impl Into<String>) -> Error {
    Error::Integrity(detail.into())
}
fn json_bytes(value: &Value) -> Result<Vec<u8>> {
    crate::result_text_and_digest(value).map(|(text, _)| text.into_bytes())
}
fn exact_fields(value: &Value, fields: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == fields.len() && fields.iter().all(|field| object.contains_key(*field))
    })
}
fn event(store_id: &str, operation: &str, body: Value) -> Result<(Vec<u8>, String)> {
    if !matches!(operation, "put" | "meta") || !body.is_object() {
        return Err(invalid("invalid Kafka command"));
    }
    let request = json_bytes(&json!({"operation":operation,"body":body}))?;
    let digest = crate::identity::hash(b"", &request);
    let operation_id = crate::identity::hash(b"pollard/kafka-operation/v1\n", &request);
    Ok((
        json_bytes(
            &json!({"version":1,"store_id":store_id,"operation_id":operation_id,
        "operation":operation,"body":body,"request_digest":digest}),
        )?,
        operation_id,
    ))
}
fn node_record(node: &Node) -> Result<Value> {
    node.validate()?;
    Ok(
        json!({"id":node.id,"parent":node.parent,"kind":node.kind,"attempt":node.attempt,
        "payload":String::from_utf8(crate::canonical_bytes(&node.payload)?).expect("canonical UTF-8"),
        "result":node.result_text,"result_digest":node.result_digest,
        "meta":String::from_utf8(json_bytes(&node.meta)?).expect("JSON UTF-8")}),
    )
}
fn parse_node(record: &Value) -> Result<Node> {
    if !exact_fields(record, &NODE_FIELDS) {
        return Err(invalid("Kafka put command has invalid fields"));
    }
    let required = |name: &str| {
        record[name]
            .as_str()
            .ok_or_else(|| invalid(format!("Kafka node requires string {name}")))
    };
    let optional = |name: &str| match &record[name] {
        Value::Null => Ok(None),
        Value::String(value) => Ok(Some(value.clone())),
        _ => Err(invalid(format!("invalid Kafka node {name}"))),
    };
    let kind = serde_json::from_value(record["kind"].clone())
        .map_err(|_| invalid("invalid Kafka node kind"))?;
    let node = Node::from_storage(
        required("id")?.into(),
        optional("parent")?,
        kind,
        record["attempt"]
            .as_u64()
            .ok_or_else(|| invalid("invalid Kafka attempt"))?,
        required("payload")?,
        optional("result")?,
        optional("result_digest")?,
        required("meta")?,
    )?;
    node.validate()?;
    Ok(node)
}

#[derive(Debug, Clone)]
pub struct KafkaOptions {
    pub topic: String,
    pub store_id: String,
    pub read_only: bool,
    pub require_existing: bool,
    pub timeout: Duration,
}
impl KafkaOptions {
    pub fn new(topic: impl Into<String>) -> Self {
        Self {
            topic: topic.into(),
            store_id: "default".into(),
            read_only: false,
            require_existing: false,
            timeout: Duration::from_secs(30),
        }
    }
    fn validate(&self) -> Result<()> {
        if self.topic.is_empty() || self.store_id.is_empty() || self.timeout.is_zero() {
            return Err(Error::Invalid(
                "Kafka topic/store ID must be nonempty and timeout positive".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Clone)]
struct Record {
    topic: String,
    partition: i32,
    offset: i64,
    key: Option<Vec<u8>>,
    value: Option<Vec<u8>>,
}
trait Transport {
    fn watermarks(&mut self) -> Result<(i64, i64)>;
    fn poll(&mut self, timeout: Duration) -> Result<Option<Record>>;
    fn produce(&mut self, key: &[u8], value: &[u8]) -> Result<i64>;
    fn enable_producer(&mut self) -> Result<()>;
    fn fresh(&self) -> Result<Box<dyn Transport>>;
}
struct DeliveryContext;
impl ClientContext for DeliveryContext {}
impl ProducerContext for DeliveryContext {
    type DeliveryOpaque = Box<mpsc::Sender<Result<(String, i32, i64)>>>;
    fn delivery(&self, result: &DeliveryResult<'_>, sender: Self::DeliveryOpaque) {
        let result = match result {
            Ok(message) => Ok((
                message.topic().to_owned(),
                message.partition(),
                message.offset(),
            )),
            Err((error, _)) => Err(invalid(format!("Kafka delivery failed: {error}"))),
        };
        let _ = sender.send(result);
    }
}
struct NativeTransport {
    config: BTreeMap<String, String>,
    options: KafkaOptions,
    consumer: BaseConsumer,
    producer: Option<BaseProducer<DeliveryContext>>,
}
impl NativeTransport {
    fn base_config(config: &BTreeMap<String, String>) -> ClientConfig {
        let mut base = ClientConfig::new();
        for (name, value) in config {
            if !CONTROLLED.contains(&name.as_str()) && name != "group.protocol" {
                base.set(name, value);
            }
        }
        base.set("allow.auto.create.topics", "false");
        base
    }
    fn open(config: BTreeMap<String, String>, options: KafkaOptions) -> Result<Self> {
        options.validate()?;
        if config
            .get("bootstrap.servers")
            .map_or(true, |value| value.is_empty())
        {
            return Err(Error::Invalid("Kafka requires bootstrap.servers".into()));
        }
        if config.contains_key("transactional.id") {
            return Err(Error::Invalid(
                "KafkaStore does not accept transactional.id".into(),
            ));
        }
        let mut base = Self::base_config(&config);
        if options.read_only {
            base.set("enable.metrics.push", "false");
        }
        let admin: AdminClient<DefaultClientContext> = base
            .create()
            .map_err(|e| invalid(format!("Kafka admin setup: {e}")))?;
        // Listing every topic avoids topic auto-creation caused by a named lookup.
        let metadata = admin
            .inner()
            .fetch_metadata(None, options.timeout)
            .map_err(|e| invalid(format!("Kafka topic metadata could not be confirmed: {e}")))?;
        let topic = metadata
            .topics()
            .iter()
            .find(|topic| topic.name() == options.topic)
            .ok_or_else(|| invalid(format!("Kafka topic does not exist: {}", options.topic)))?;
        if topic.error().is_some()
            || topic.partitions().len() != 1
            || topic.partitions()[0].id() != 0
            || topic.partitions()[0].error().is_some()
        {
            return Err(invalid(
                "KafkaStore requires exactly partition zero with available metadata",
            ));
        }
        let resources = [ResourceSpecifier::Topic(&options.topic)];
        let configs = futures_executor::block_on(admin.describe_configs(
            &resources,
            &AdminOptions::new().request_timeout(Some(options.timeout)),
        ))
        .map_err(|e| {
            invalid(format!(
                "Kafka topic configuration could not be confirmed: {e}"
            ))
        })?;
        let resource = configs
            .into_iter()
            .next()
            .ok_or_else(|| invalid("Kafka topic configuration is missing"))?
            .map_err(|e| invalid(format!("Kafka topic configuration unavailable: {e:?}")))?;
        let values: BTreeMap<_, _> = resource
            .entries
            .into_iter()
            .filter_map(|entry| entry.value.map(|value| (entry.name, value)))
            .collect();
        validate_topic_config(&values)?;
        let identity = crate::identity::hash(
            b"",
            format!("{}\0{}", options.topic, options.store_id).as_bytes(),
        );
        base.set(
            "group.id",
            format!(
                "pollard-{}-{identity}",
                if options.read_only {
                    "observer"
                } else {
                    "reader"
                }
            ),
        )
        .set("group.protocol", "classic")
        .set("enable.auto.commit", "false")
        .set("enable.auto.offset.store", "false")
        .set("enable.partition.eof", "false")
        .set("auto.offset.reset", "earliest")
        .set("isolation.level", "read_committed");
        let consumer: BaseConsumer = base
            .create()
            .map_err(|e| invalid(format!("Kafka consumer setup: {e}")))?;
        let mut assignment = TopicPartitionList::new();
        assignment
            .add_partition_offset(&options.topic, 0, Offset::Beginning)
            .map_err(|e| invalid(e.to_string()))?;
        consumer
            .assign(&assignment)
            .map_err(|e| invalid(format!("Kafka partition assignment: {e}")))?;
        Ok(Self {
            config,
            options,
            consumer,
            producer: None,
        })
    }
}
fn validate_topic_config(values: &BTreeMap<String, String>) -> Result<()> {
    let cleanup: BTreeSet<_> = values
        .get("cleanup.policy")
        .map(|value| {
            value
                .split(',')
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .collect()
        })
        .unwrap_or_default();
    if cleanup != BTreeSet::from(["delete"]) {
        return Err(invalid(
            "KafkaStore requires cleanup.policy=delete without log compaction",
        ));
    }
    for field in ["retention.ms", "retention.bytes"] {
        if values.get(field).map(String::as_str) != Some("-1") {
            return Err(invalid(format!("KafkaStore requires {field}=-1")));
        }
    }
    Ok(())
}
impl Transport for NativeTransport {
    fn watermarks(&mut self) -> Result<(i64, i64)> {
        self.consumer
            .fetch_watermarks(&self.options.topic, 0, self.options.timeout)
            .map_err(|e| invalid(format!("Kafka watermarks could not be confirmed: {e}")))
    }
    fn poll(&mut self, timeout: Duration) -> Result<Option<Record>> {
        self.consumer
            .poll(timeout)
            .map(|result| {
                result
                    .map(|message| Record {
                        topic: message.topic().into(),
                        partition: message.partition(),
                        offset: message.offset(),
                        key: message.key().map(<[u8]>::to_vec),
                        value: message.payload().map(<[u8]>::to_vec),
                    })
                    .map_err(|e| invalid(format!("Kafka replay failed: {e}")))
            })
            .transpose()
    }
    fn enable_producer(&mut self) -> Result<()> {
        if self.options.read_only {
            return Err(invalid("read-only KafkaStore cannot create producer"));
        }
        let mut config = Self::base_config(&self.config);
        config.set("acks", "all").set("enable.idempotence", "true");
        self.producer = Some(
            config
                .create_with_context(DeliveryContext)
                .map_err(|e| invalid(format!("Kafka producer setup: {e}")))?,
        );
        Ok(())
    }
    fn produce(&mut self, key: &[u8], value: &[u8]) -> Result<i64> {
        let producer = self
            .producer
            .as_ref()
            .ok_or_else(|| invalid("KafkaStore has no producer"))?;
        let (send, receive) = mpsc::channel();
        producer
            .send(
                BaseRecord::with_opaque_to(&self.options.topic, Box::new(send))
                    .partition(0)
                    .key(key)
                    .payload(value),
            )
            .map_err(|(error, _)| {
                invalid(format!("Kafka command could not be enqueued: {error}"))
            })?;
        let start = Instant::now();
        loop {
            match receive.try_recv() {
                Ok(result) => {
                    let (topic, partition, offset) = result?;
                    if topic != self.options.topic || partition != 0 || offset < 0 {
                        return Err(invalid("Kafka delivered command to an invalid location"));
                    }
                    return Ok(offset);
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    return Err(invalid("Kafka delivery callback disconnected"))
                }
                Err(mpsc::TryRecvError::Empty) => (),
            }
            let remaining = self
                .options
                .timeout
                .checked_sub(start.elapsed())
                .ok_or_else(|| invalid("Kafka delivery acknowledgment timed out"))?;
            producer.poll(remaining.min(Duration::from_millis(100)));
        }
    }
    fn fresh(&self) -> Result<Box<dyn Transport>> {
        Ok(Box::new(Self::open(
            self.config.clone(),
            self.options.clone(),
        )?))
    }
}

#[derive(Clone, Default)]
struct View {
    nodes: MemoryStore,
    operations: BTreeMap<String, (String, i64, Result<()>)>,
    digests: Vec<String>,
    log: Vec<Vec<u8>>,
    next_offset: i64,
    snapshot_high: Option<i64>,
}
impl View {
    fn apply(&mut self, record: Record, options: &KafkaOptions) -> Result<()> {
        if record.topic != options.topic || record.partition != 0 {
            return Err(invalid("Kafka replay crossed configured topic partition"));
        }
        if record.offset != self.next_offset {
            return Err(invalid(format!(
                "Kafka log has offset gap: expected {}, received {}",
                self.next_offset, record.offset
            )));
        }
        let key = record
            .key
            .ok_or_else(|| invalid("Kafka log has no store key"))?;
        if key != options.store_id.as_bytes() {
            return Err(invalid("Kafka log has wrong store key"));
        }
        let value = record
            .value
            .ok_or_else(|| invalid("Kafka log has no byte record"))?;
        let envelope: Value = serde_json::from_slice(&value)
            .map_err(|e| invalid(format!("Kafka log is not JSON: {e}")))?;
        if !exact_fields(&envelope, &EVENT_FIELDS)
            || envelope["version"].as_f64() != Some(1.0)
            || envelope["store_id"].as_str() != Some(options.store_id.as_str())
        {
            return Err(invalid(
                "Kafka log has invalid envelope fields or version/store ID",
            ));
        }
        let operation = envelope["operation"]
            .as_str()
            .ok_or_else(|| invalid("Kafka log has invalid operation"))?;
        let body = &envelope["body"];
        let (expected, id) = event(&options.store_id, operation, body.clone())?;
        let expected: Value = serde_json::from_slice(&expected).expect("generated envelope");
        if envelope["operation_id"] != expected["operation_id"]
            || envelope["request_digest"] != expected["request_digest"]
            || value != json_bytes(&envelope)?
        {
            return Err(invalid("Kafka log failed canonical envelope validation"));
        }
        let digest = envelope["request_digest"]
            .as_str()
            .expect("validated digest")
            .to_owned();
        if let Some((previous, _, _)) = self.operations.get(&id) {
            if previous != &digest {
                return Err(invalid("Kafka operation ID collision"));
            }
        } else {
            let outcome = match operation {
                "put" => self.nodes.put(parse_node(body)?),
                "meta" => {
                    if !exact_fields(body, &["id", "patch"]) || !body["patch"].is_object() {
                        return Err(invalid("Kafka log has invalid metadata patch"));
                    }
                    let node_id = body["id"]
                        .as_str()
                        .ok_or_else(|| invalid("Kafka metadata ID must be string"))?;
                    self.nodes.update_meta(node_id, body["patch"].clone())
                }
                _ => unreachable!("event validated operation"),
            };
            self.operations.insert(id, (digest, record.offset, outcome));
        }
        let mut framed = (key.len() as u64).to_be_bytes().to_vec();
        framed.extend(key);
        framed.extend(&value);
        self.digests.push(crate::identity::hash(b"", &framed));
        self.log.push(value);
        self.next_offset += 1;
        Ok(())
    }
}
struct Session {
    transport: Option<Box<dyn Transport>>,
    view: View,
    staged: BTreeMap<String, Node>,
}
impl Session {
    fn require_open(&self) -> Result<()> {
        if self.transport.is_none() {
            Err(invalid("KafkaStore is closed"))
        } else {
            Ok(())
        }
    }
    fn sync(&mut self, options: &KafkaOptions) -> Result<()> {
        self.require_open()?;
        if options.read_only && self.view.snapshot_high.is_some() {
            return Ok(());
        }
        let (low, high) = self.transport.as_mut().expect("open").watermarks()?;
        if low != 0 {
            return Err(invalid(
                "Kafka log start is not offset zero; history was truncated",
            ));
        }
        if high < self.view.next_offset {
            return Err(invalid("Kafka high watermark moved behind replay cursor"));
        }
        if high > self.view.next_offset {
            self.sync_to(high - 1, options)?;
        }
        if options.read_only {
            self.view.snapshot_high = Some(high);
        }
        Ok(())
    }
    fn sync_to(&mut self, target: i64, options: &KafkaOptions) -> Result<()> {
        let started = Instant::now();
        while self.view.next_offset <= target {
            let remaining = options
                .timeout
                .checked_sub(started.elapsed())
                .ok_or_else(|| invalid(format!("Kafka replay timed out before offset {target}")))?;
            if let Some(record) = self
                .transport
                .as_mut()
                .ok_or_else(|| invalid("KafkaStore is closed"))?
                .poll(remaining.min(Duration::from_millis(250)))?
            {
                self.view.apply(record, options)?;
            }
        }
        Ok(())
    }
    fn reconnect(&mut self, options: &KafkaOptions) -> Result<()> {
        let transport = self
            .transport
            .as_ref()
            .ok_or_else(|| invalid("KafkaStore is closed"))?
            .fresh()?;
        let mut fresh = Self {
            transport: Some(transport),
            view: View::default(),
            staged: self.staged.clone(),
        };
        fresh.sync(options)?;
        if !fresh.view.digests.starts_with(&self.view.digests) {
            return Err(invalid(
                "Kafka history changed before prior replay boundary",
            ));
        }
        if options.require_existing && fresh.view.nodes.roots()?.is_empty() {
            return Err(invalid("Kafka logical store has no materialized nodes"));
        }
        if !options.read_only {
            fresh.transport.as_mut().expect("open").enable_producer()?;
        }
        *self = fresh;
        Ok(())
    }
    fn append(&mut self, operation: &str, body: Value, options: &KafkaOptions) -> Result<()> {
        self.require_open()?;
        if options.read_only {
            return Err(invalid("KafkaStore is read-only"));
        }
        self.sync(options)?;
        let (bytes, id) = event(&options.store_id, operation, body)?;
        if let Some((_, _, outcome)) = self.view.operations.get(&id) {
            return outcome.clone();
        }
        let mut delivered = None;
        for _ in 0..2 {
            match self
                .transport
                .as_mut()
                .expect("open")
                .produce(options.store_id.as_bytes(), &bytes)
            {
                Ok(offset) if offset >= 0 => {
                    delivered = Some(offset);
                    break;
                }
                _ => {
                    let _ = self.sync(options);
                    if let Some((_, offset, _)) = self.view.operations.get(&id) {
                        delivered = Some(*offset);
                        break;
                    }
                }
            }
        }
        if delivered.is_none() {
            let _ = self.sync(options);
            delivered = self.view.operations.get(&id).map(|(_, offset, _)| *offset);
        }
        let target = delivered.ok_or_else(|| {
            invalid(format!(
                "Kafka write outcome is uncertain for operation {id}"
            ))
        })?;
        if self.sync_to(target, options).is_err() {
            self.reconnect(options).map_err(|error|invalid(format!("Kafka command was acknowledged but replay confirmation failed for {id}: {error}")))?;
        }
        self.view
            .operations
            .get(&id)
            .ok_or_else(|| {
                invalid(format!(
                    "Kafka acknowledged operation absent after replay: {id}"
                ))
            })?
            .2
            .clone()
    }
    fn node(&self, id: &str) -> Result<Node> {
        self.staged
            .get(id)
            .cloned()
            .map(Ok)
            .unwrap_or_else(|| self.view.nodes.get(id))
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        let mut children: BTreeSet<_> = self.view.nodes.children(id)?.into_iter().collect();
        children.extend(
            self.staged
                .values()
                .filter(|node| node.parent.as_deref() == Some(id))
                .map(|node| node.id.clone()),
        );
        let mut children: Vec<_> = children.into_iter().collect();
        children.sort_by_key(|id| {
            self.node(id)
                .map(|node| (node.kind.as_str().to_owned(), node.id))
                .unwrap_or_default()
        });
        Ok(children)
    }
}

pub struct KafkaStore {
    options: KafkaOptions,
    session: RefCell<Session>,
}
impl KafkaStore {
    pub fn open(client_config: BTreeMap<String, String>, options: KafkaOptions) -> Result<Self> {
        let transport = Box::new(NativeTransport::open(client_config, options.clone())?);
        Self::from_transport(transport, options)
    }
    fn from_transport(transport: Box<dyn Transport>, options: KafkaOptions) -> Result<Self> {
        options.validate()?;
        let mut session = Session {
            transport: Some(transport),
            view: View::default(),
            staged: BTreeMap::new(),
        };
        session.sync(&options)?;
        if options.require_existing && session.view.nodes.roots()?.is_empty() {
            return Err(invalid(
                "Kafka logical store has no materialized nodes; identity cannot be confirmed",
            ));
        }
        if !options.read_only {
            session
                .transport
                .as_mut()
                .expect("open")
                .enable_producer()?;
        }
        Ok(Self {
            options,
            session: RefCell::new(session),
        })
    }
    pub fn options(&self) -> &KafkaOptions {
        &self.options
    }
    pub fn reconnect(&self) -> Result<()> {
        self.session.borrow_mut().reconnect(&self.options)
    }
    pub fn close(&self) {
        self.session.borrow_mut().transport.take();
    }
    fn writable(&self) -> Result<()> {
        self.session.borrow().require_open()?;
        if self.options.read_only {
            return Err(invalid("KafkaStore is read-only"));
        }
        Ok(())
    }
}
impl Store for KafkaStore {
    fn put(&mut self, node: Node) -> Result<()> {
        self.writable()?;
        let body = node_record(&node)?;
        let mut session = self.session.borrow_mut();
        session.sync(&self.options)?;
        if session.staged.contains_key(&node.id) {
            return Err(invalid(
                "cannot put in-flight Kafka identity before finalization",
            ));
        }
        if let Some(parent) = &node.parent {
            session.node(parent)?;
        }
        session.append("put", body, &self.options)
    }
    fn get(&self, id: &str) -> Result<Node> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        s.node(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.try_exists(id).unwrap_or(false)
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        Ok(s.staged.contains_key(id) || s.view.nodes.exists(id))
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        s.children(id)
    }
    fn roots(&self) -> Result<Vec<String>> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        s.view.nodes.roots()
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.writable()?;
        if !patch.is_object() {
            return Err(Error::Invalid("Kafka metadata patch must be object".into()));
        }
        json_bytes(&patch)?;
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        if let Some(node) = s.staged.get_mut(id) {
            node.meta
                .as_object_mut()
                .expect("object metadata")
                .extend(patch.as_object().expect("validated patch").clone());
            return Ok(());
        }
        s.node(id)?;
        s.append("meta", json!({"id":id,"patch":patch}), &self.options)
    }
    fn walk(&self, root: &str) -> Result<Vec<Node>> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        let mut pending = vec![root.to_owned()];
        let mut seen = BTreeSet::new();
        let mut nodes = Vec::new();
        while let Some(id) = pending.pop() {
            if !seen.insert(id.clone()) {
                return Err(invalid("Kafka tree contains a cycle"));
            }
            nodes.push(s.node(&id)?);
            pending.extend(s.children(&id)?.into_iter().rev());
        }
        Ok(nodes)
    }
    fn operation_log(&self) -> Result<Vec<u8>> {
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        Ok(s.view
            .log
            .iter()
            .flat_map(|value| value.iter().copied().chain(*b"\n"))
            .collect())
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.writable()?;
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        if ids.iter().any(|id| !s.staged.contains_key(id)) {
            return Err(invalid(
                "Kafka audit history cannot be deleted or compacted",
            ));
        }
        if s.staged.values().any(|node| {
            !ids.contains(&node.id)
                && node
                    .parent
                    .as_ref()
                    .is_some_and(|parent| ids.contains(parent))
        }) {
            return Err(invalid(
                "cannot remove a pending ancestor of retained Kafka nodes",
            ));
        }
        for id in ids {
            s.staged.remove(id);
        }
        Ok(())
    }
}
impl RecordingStore for KafkaStore {
    fn stage_pending(&mut self, node: Node) -> Result<()> {
        self.writable()?;
        node.validate()?;
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        if node.result.is_some()
            || node.meta["state"] != json!("pending")
            || s.staged.contains_key(&node.id)
            || s.view.nodes.exists(&node.id)
        {
            return Err(invalid("Kafka stage requires a new pending identity"));
        }
        if let Some(parent) = &node.parent {
            s.node(parent)?;
        }
        s.staged.insert(node.id.clone(), node);
        Ok(())
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.writable()?;
        let body = node_record(&node)?;
        let mut s = self.session.borrow_mut();
        s.sync(&self.options)?;
        let pending = s
            .staged
            .get(&node.id)
            .ok_or_else(|| invalid("Kafka identity was not staged"))?;
        if pending.kind != node.kind
            || pending.parent != node.parent
            || pending.attempt != node.attempt
            || pending.payload != node.payload
            || !matches!(node.meta["state"].as_str(), Some("completed" | "failed"))
        {
            return Err(invalid("Kafka settlement identity/state mismatch"));
        }
        s.append("put", body, &self.options)?;
        s.staged.remove(&node.id);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::rc::Rc;
    #[derive(Default)]
    struct Broker {
        records: Vec<Record>,
        low: i64,
        high_override: Option<i64>,
        watermarks_fail: usize,
        polls_fail: usize,
        poll_delay: Duration,
        fresh_fail: bool,
        produce_fail: usize,
        commit_on_error: bool,
        acknowledge_without_append: bool,
        produces: usize,
        producers_opened: usize,
    }
    struct MockTransport {
        broker: Rc<RefCell<Broker>>,
        cursor: usize,
        producer: bool,
    }
    impl Transport for MockTransport {
        fn watermarks(&mut self) -> Result<(i64, i64)> {
            let mut broker = self.broker.borrow_mut();
            if broker.watermarks_fail > 0 {
                broker.watermarks_fail -= 1;
                return Err(invalid("watermarks unavailable"));
            }
            Ok((
                broker.low,
                broker.high_override.unwrap_or(broker.records.len() as i64),
            ))
        }
        fn poll(&mut self, _: Duration) -> Result<Option<Record>> {
            let mut broker = self.broker.borrow_mut();
            if broker.polls_fail > 0 {
                broker.polls_fail -= 1;
                return Err(invalid("consumer disconnected"));
            }
            let record = broker.records.get(self.cursor).cloned();
            if record.is_some() {
                self.cursor += 1;
                std::thread::sleep(broker.poll_delay);
            }
            Ok(record)
        }
        fn enable_producer(&mut self) -> Result<()> {
            self.broker.borrow_mut().producers_opened += 1;
            self.producer = true;
            Ok(())
        }
        fn produce(&mut self, key: &[u8], value: &[u8]) -> Result<i64> {
            assert!(self.producer);
            let mut broker = self.broker.borrow_mut();
            broker.produces += 1;
            let offset = broker.records.len() as i64;
            let fail = broker.produce_fail > 0;
            if fail {
                broker.produce_fail -= 1;
            }
            if !broker.acknowledge_without_append && (!fail || broker.commit_on_error) {
                broker.records.push(Record {
                    topic: "audit".into(),
                    partition: 0,
                    offset,
                    key: Some(key.to_vec()),
                    value: Some(value.to_vec()),
                });
            }
            if fail {
                Err(invalid("acknowledgment lost"))
            } else {
                Ok(offset)
            }
        }
        fn fresh(&self) -> Result<Box<dyn Transport>> {
            if self.broker.borrow().fresh_fail {
                return Err(invalid("reconnect unavailable"));
            }
            Ok(Box::new(Self {
                broker: self.broker.clone(),
                cursor: 0,
                producer: false,
            }))
        }
    }
    fn options() -> KafkaOptions {
        KafkaOptions::new("audit")
    }
    fn missing_record_options() -> KafkaOptions {
        KafkaOptions {
            timeout: Duration::from_millis(5),
            ..options()
        }
    }
    fn open(broker: &Rc<RefCell<Broker>>, options: KafkaOptions) -> Result<KafkaStore> {
        KafkaStore::from_transport(
            Box::new(MockTransport {
                broker: broker.clone(),
                cursor: 0,
                producer: false,
            }),
            options,
        )
    }
    fn root() -> Node {
        Node::make(
            crate::NodeKind::Root,
            None,
            0,
            json!({"run":"kafka"}),
            None,
            json!({}),
        )
        .unwrap()
    }
    fn append_raw(broker: &Rc<RefCell<Broker>>, key: &str, value: Vec<u8>) {
        let mut b = broker.borrow_mut();
        let offset = b.records.len() as i64;
        b.records.push(Record {
            topic: "audit".into(),
            partition: 0,
            offset,
            key: Some(key.as_bytes().to_vec()),
            value: Some(value),
        });
    }

    #[test]
    fn kafka_wire_bytes_match_pypi_160_oracle() {
        let fixtures: Value =
            serde_json::from_str(include_str!("../tests/pypi160_kafka.json")).unwrap();
        for row in fixtures["events"].as_array().unwrap() {
            let store_id = row["store_id"].as_str().unwrap();
            let (raw, id) = event(
                store_id,
                row["operation"].as_str().unwrap(),
                row["body"].clone(),
            )
            .unwrap();
            assert_eq!(id, row["operation_id"].as_str().unwrap());
            assert_eq!(
                std::str::from_utf8(&raw).unwrap(),
                row["event_utf8"].as_str().unwrap()
            );
            let key = store_id.as_bytes();
            let mut framed = (key.len() as u64).to_be_bytes().to_vec();
            framed.extend(key);
            framed.extend(raw);
            assert_eq!(
                crate::identity::hash(b"", &framed),
                row["record_digest"].as_str().unwrap()
            );
            if row["operation"] == json!("put") {
                assert_eq!(
                    node_record(&parse_node(&row["body"]).unwrap()).unwrap(),
                    row["body"]
                );
            }
        }
    }

    #[test]
    fn kafka_roundtrip_idempotency_conflicts_and_ordered_metadata() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, options()).unwrap();
        let root = root();
        store.put(root.clone()).unwrap();
        store.put(root.clone()).unwrap();
        assert_eq!(broker.borrow().records.len(), 1);
        let child = Node::make(
            crate::NodeKind::ModelCall,
            Some(&root.id),
            0,
            json!({"model":"m"}),
            Some(json!({"text":"α","number":1.25})),
            json!({"charges":{"steps":1}}),
        )
        .unwrap();
        store.put(child.clone()).unwrap();
        let conflict = Node::make(
            child.kind,
            child.parent.as_deref(),
            0,
            child.payload.clone(),
            Some(json!({"text":"β"})),
            json!({}),
        )
        .unwrap();
        store.put(conflict.clone()).unwrap();
        store.put(conflict).unwrap();
        store
            .update_meta(&child.id, json!({"observed":true}))
            .unwrap();
        let recorded = store.get(&child.id).unwrap();
        assert_eq!(recorded.result, child.result);
        assert_eq!(
            recorded.meta["result_conflicts"].as_array().unwrap().len(),
            1
        );
        assert_eq!(recorded.meta["observed"], json!(true));
        assert_eq!(broker.borrow().records.len(), 4);
        assert_eq!(
            store.children(&root.id).unwrap(),
            std::slice::from_ref(&child.id)
        );
        assert_eq!(store.walk(&root.id).unwrap().len(), 2);
        assert_eq!(
            open(&broker, options()).unwrap().get(&child.id).unwrap(),
            recorded
        );
        assert!(!store.supports_reservations());
    }
    #[test]
    fn kafka_uncertain_ack_recovers_and_unobserved_commands_fail_explicitly() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, options()).unwrap();
        broker.borrow_mut().produce_fail = 1;
        broker.borrow_mut().commit_on_error = true;
        store.put(root()).unwrap();
        assert_eq!(broker.borrow().produces, 1);
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, options()).unwrap();
        broker.borrow_mut().produce_fail = 2;
        let error = store.put(root()).unwrap_err().to_string();
        assert!(error.contains("outcome is uncertain for operation"));
        assert_eq!(broker.borrow().produces, 2);
        assert!(store.roots().unwrap().is_empty());
    }
    #[test]
    fn kafka_acknowledged_replay_failure_reconnects_before_success() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, options()).unwrap();
        broker.borrow_mut().polls_fail = 1;
        store.put(root()).unwrap();
        assert_eq!(broker.borrow().producers_opened, 2);
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, missing_record_options()).unwrap();
        broker.borrow_mut().acknowledge_without_append = true;
        assert!(store
            .put(root())
            .unwrap_err()
            .to_string()
            .contains("absent after replay"));
    }
    #[test]
    fn kafka_readonly_freezes_snapshot_and_reconnect_is_atomic_with_prefix_check() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut writer = open(&broker, options()).unwrap();
        writer.put(root()).unwrap();
        let mut opts = options();
        opts.read_only = true;
        opts.require_existing = true;
        let mut reader = open(&broker, opts).unwrap();
        let before = broker.borrow().producers_opened;
        writer
            .update_meta(&root().id, json!({"version":2}))
            .unwrap();
        assert!(reader
            .get(&root().id)
            .unwrap()
            .meta
            .get("version")
            .is_none());
        assert!(reader.put(root()).is_err());
        assert_eq!(broker.borrow().producers_opened, before);
        reader.reconnect().unwrap();
        assert_eq!(reader.get(&root().id).unwrap().meta["version"], json!(2));
        let original = broker.borrow().records[0].value.clone();
        let mut changed = root();
        changed.meta = json!({"replaced":true});
        broker.borrow_mut().records[0].value = Some(
            event("default", "put", node_record(&changed).unwrap())
                .unwrap()
                .0,
        );
        assert!(reader
            .reconnect()
            .unwrap_err()
            .to_string()
            .contains("history changed"));
        assert_eq!(reader.get(&root().id).unwrap().meta["version"], json!(2));
        broker.borrow_mut().records[0].value = original;
        reader.close();
        reader.close();
        assert!(reader.get(&root().id).is_err());
        assert!(reader.reconnect().is_err());
    }
    #[test]
    fn kafka_detects_truncated_watermarks_offset_gaps_and_wrong_partition() {
        for fault in 0..5 {
            let broker = Rc::new(RefCell::new(Broker::default()));
            let mut store = open(&broker, options()).unwrap();
            store.put(root()).unwrap();
            match fault {
                0 => broker.borrow_mut().low = 1,
                1 => broker.borrow_mut().high_override = Some(0),
                _ => {
                    append_raw(
                        &broker,
                        "default",
                        event("default", "meta", json!({"id":root().id,"patch":{}}))
                            .unwrap()
                            .0,
                    );
                    let mut b = broker.borrow_mut();
                    match fault {
                        2 => b.records[1].offset = 2,
                        3 => b.records[1].partition = 1,
                        _ => b.records[1].topic = "wrong".into(),
                    }
                }
            }
            assert!(store.try_exists(&root().id).is_err());
        }
    }
    #[test]
    fn kafka_envelopes_reject_noncanonical_tampered_or_unscoped_messages() {
        let canonical = event("default", "put", node_record(&root()).unwrap())
            .unwrap()
            .0;
        for fault in 0..9 {
            let broker = Rc::new(RefCell::new(Broker::default()));
            let mut envelope: Value = serde_json::from_slice(&canonical).unwrap();
            let mut key = "default";
            let mut raw = None;
            match fault {
                0 => key = "wrong",
                1 => envelope["version"] = json!(true),
                2 => envelope["store_id"] = json!("wrong"),
                3 => envelope["extra"] = json!(true),
                4 => envelope["operation_id"] = json!("f".repeat(64)),
                5 => envelope["body"]["meta"] = json!("{\"tampered\":true}"),
                6 => raw = Some([canonical.clone(), vec![b' ']].concat()),
                7 => {
                    let mut body = node_record(&root()).unwrap();
                    body["extra"] = json!(0);
                    raw = Some(event("default", "put", body).unwrap().0);
                }
                _ => {
                    raw = Some(
                        event(
                            "default",
                            "meta",
                            json!({"id":root().id,"patch":{},"extra":true}),
                        )
                        .unwrap()
                        .0,
                    )
                }
            }
            append_raw(
                &broker,
                key,
                raw.unwrap_or_else(|| json_bytes(&envelope).unwrap()),
            );
            assert!(open(&broker, options()).is_err(), "fault {fault}");
        }
    }
    #[test]
    fn kafka_logical_missing_parent_outcome_does_not_poison_later_valid_records() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let orphan = Node::make(
            crate::NodeKind::Note,
            Some(&root().id),
            0,
            json!({}),
            None,
            json!({}),
        )
        .unwrap();
        append_raw(
            &broker,
            "default",
            event("default", "put", node_record(&orphan).unwrap())
                .unwrap()
                .0,
        );
        append_raw(
            &broker,
            "default",
            event("default", "put", node_record(&root()).unwrap())
                .unwrap()
                .0,
        );
        let mut store = open(&broker, options()).unwrap();
        assert!(!store.try_exists(&orphan.id).unwrap());
        assert!(matches!(store.put(orphan), Err(Error::NotFound(_))));
        assert_eq!(store.roots().unwrap(), [root().id]);
    }
    #[test]
    fn kafka_functional_replay_tolerates_poll_delays_beyond_old_fixture_deadline() {
        let broker = Rc::new(RefCell::new(Broker {
            // A delay between two available records deterministically crosses
            // the old 5 ms fixture deadline without timing the assertion.
            poll_delay: Duration::from_millis(20),
            ..Broker::default()
        }));
        let child = Node::make(
            crate::NodeKind::Note,
            Some(&root().id),
            0,
            json!({"delayed": true}),
            None,
            json!({}),
        )
        .unwrap();
        for node in [root(), child.clone()] {
            append_raw(
                &broker,
                "default",
                event("default", "put", node_record(&node).unwrap())
                    .unwrap()
                    .0,
            );
        }
        assert_eq!(options().timeout, Duration::from_secs(30));
        let store = open(&broker, options()).unwrap();
        assert_eq!(store.get(&child.id).unwrap(), child);
        assert_eq!(store.roots().unwrap(), [root().id]);
    }
    #[test]
    fn kafka_missing_record_times_out_before_enabling_producer() {
        let broker = Rc::new(RefCell::new(Broker {
            high_override: Some(1),
            ..Broker::default()
        }));
        let error = match open(&broker, missing_record_options()) {
            Ok(_) => panic!("missing offset must fail closed"),
            Err(error) => error,
        };
        assert_eq!(error, invalid("Kafka replay timed out before offset 0"));
        assert_eq!(broker.borrow().producers_opened, 0);
        assert_eq!(broker.borrow().produces, 0);
    }
    #[test]
    fn kafka_staging_publishes_only_settlement_and_only_transient_nodes_can_drop() {
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut store = open(&broker, options()).unwrap();
        store.put(root()).unwrap();
        let pending = Node::make(
            crate::NodeKind::ModelCall,
            Some(&root().id),
            0,
            json!({}),
            None,
            json!({"state":"pending"}),
        )
        .unwrap();
        store.stage_pending(pending.clone()).unwrap();
        assert_eq!(
            store.children(&root().id).unwrap(),
            std::slice::from_ref(&pending.id)
        );
        assert_eq!(broker.borrow().records.len(), 1);
        store
            .drop_nodes(&BTreeSet::from([pending.id.clone()]))
            .unwrap();
        assert!(!store.try_exists(&pending.id).unwrap());
        store.stage_pending(pending.clone()).unwrap();
        let completed = Node::make(
            pending.kind,
            pending.parent.as_deref(),
            0,
            pending.payload,
            Some(json!({"done":true})),
            json!({"state":"completed"}),
        )
        .unwrap();
        store.finalize(completed.clone()).unwrap();
        assert_eq!(broker.borrow().records.len(), 2);
        assert_eq!(
            open(&broker, options())
                .unwrap()
                .get(&completed.id)
                .unwrap(),
            completed
        );
        assert!(store.drop_nodes(&BTreeSet::from([completed.id])).is_err());
    }
    #[test]
    fn kafka_requires_full_history_topic_config_and_existing_before_producer() {
        let config = BTreeMap::from([
            ("cleanup.policy".into(), "delete".into()),
            ("retention.ms".into(), "-1".into()),
            ("retention.bytes".into(), "-1".into()),
        ]);
        validate_topic_config(&config).unwrap();
        for (name, value) in [
            ("cleanup.policy", "compact"),
            ("cleanup.policy", "delete,compact"),
            ("retention.ms", "1000"),
            ("retention.bytes", "1000"),
        ] {
            let mut invalid = config.clone();
            invalid.insert(name.into(), value.into());
            assert!(validate_topic_config(&invalid).is_err());
        }
        let broker = Rc::new(RefCell::new(Broker::default()));
        let mut opts = options();
        opts.require_existing = true;
        assert!(open(&broker, opts).is_err());
        assert_eq!(broker.borrow().producers_opened, 0);
    }

    #[test]
    #[cfg(feature = "kafka-tls")]
    fn kafka_tls_driver_includes_ssl_and_builtin_sasl_scram_plain() {
        // Configuration validation proves these mechanisms were compiled in;
        // this does not claim a remote TLS/authentication integration test.
        let producer: BaseProducer = ClientConfig::new()
            .set("builtin.features", "ssl,sasl_plain,sasl_scram")
            .set("security.protocol", "SASL_SSL")
            .set("sasl.mechanism", "SCRAM-SHA-256")
            .set("sasl.username", "fixture")
            .set("sasl.password", "fixture")
            .create()
            .unwrap();
        drop(producer);
    }
}
