//! In-process append-only operation log compatible with Python HashRopeStore.
//!
//! Immutable byte chunks avoid copying the accumulated log on append. The
//! cached polynomial hash uses hashrope's default base 131 and prime 2^61-1.
//! It is an indexing checksum, not a cryptographic integrity guarantee; verify
//! node identities/result digests and use Pollard seals for audit boundaries.
use crate::{
    canonical_bytes, result_text_and_digest, Error, MemoryStore, Node, RecordingStore, Result,
    Store, Value,
};
use serde_json::json;
use std::collections::BTreeSet;

const PRIME: u128 = (1u128 << 61) - 1;
fn extend_hash(mut hash: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        hash = ((u128::from(hash) * 131 + u128::from(*byte) + 1) % PRIME) as u64;
    }
    hash
}
fn text(value: &Value) -> Result<String> {
    result_text_and_digest(value).map(|(text, _)| text)
}
fn put_record(node: &Node) -> Result<Vec<u8>> {
    let payload = String::from_utf8(canonical_bytes(&node.payload)?).expect("canonical UTF-8");
    record_bytes(&json!({"op":"put","id":node.id,"parent":node.parent,
        "kind":node.kind,"attempt":node.attempt,"payload":payload,
        "result":node.result_text,"result_digest":node.result_digest,"meta":text(&node.meta)?}))
}
fn record_bytes(record: &Value) -> Result<Vec<u8>> {
    let mut bytes = text(record)?.into_bytes();
    bytes.push(b'\n');
    Ok(bytes)
}
fn parse_node(record: &Value) -> Result<Node> {
    let required = |name: &str| {
        record
            .get(name)
            .and_then(Value::as_str)
            .ok_or_else(|| Error::Integrity(format!("hashrope put record requires string {name}")))
    };
    let optional = |name: &str| match record.get(name) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(Error::Integrity(format!(
            "hashrope put record has invalid {name}"
        ))),
    };
    let kind = serde_json::from_value(record.get("kind").cloned().unwrap_or(Value::Null))
        .map_err(|_| Error::Integrity("hashrope put record has invalid kind".into()))?;
    Node::from_storage(
        required("id")?.to_owned(),
        optional("parent")?,
        kind,
        record
            .get("attempt")
            .and_then(Value::as_u64)
            .ok_or_else(|| Error::Integrity("hashrope put record has invalid attempt".into()))?,
        required("payload")?,
        optional("result")?,
        optional("result_digest")?,
        required("meta")?,
    )
}

#[derive(Clone, Default)]
pub struct HashRopeStore {
    inner: MemoryStore,
    chunks: Vec<Vec<u8>>,
    hash: u64,
    length: usize,
    staged: BTreeSet<String>,
}
impl HashRopeStore {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn from_bytes(data: &[u8]) -> Result<Self> {
        let mut store = Self::new();
        // Python bytes.splitlines recognizes LF, CRLF and bare CR.
        for (index, line) in data.split(|byte| matches!(byte, b'\n' | b'\r')).enumerate() {
            if line.is_empty() {
                continue;
            }
            let record: Value = serde_json::from_slice(line)
                .map_err(|e| Error::Integrity(format!("hashrope log line {}: {e}", index + 1)))?;
            match record.get("op").and_then(Value::as_str) {
                Some("put") => store.inner.put(parse_node(&record)?)?,
                Some("meta") => {
                    let id = record
                        .get("id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| Error::Integrity("invalid hashrope meta id".into()))?;
                    let patch = record
                        .get("patch")
                        .filter(|v| v.is_object())
                        .ok_or_else(|| Error::Integrity("invalid hashrope meta patch".into()))?;
                    crate::identity::validate_finite_json(patch)?;
                    store.inner.update_meta(id, patch.clone())?;
                }
                _ => {
                    return Err(Error::Integrity(format!(
                        "hashrope log line {} has unknown operation",
                        index + 1
                    )))
                }
            }
        }
        if !data.is_empty() {
            store.append(data.to_vec());
        }
        Ok(store)
    }
    /// Serialize persisted operations. In-flight calls are transient, as in Python.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(self.length);
        for chunk in &self.chunks {
            bytes.extend_from_slice(chunk);
        }
        bytes
    }
    pub fn content_hash(&self) -> u64 {
        self.hash
    }
    pub fn validate_log(&self) -> Result<()> {
        let data = self.to_bytes();
        if extend_hash(0, &data) != self.hash || data.len() != self.length {
            return Err(Error::Integrity(
                "hashrope cached hash/length mismatch".into(),
            ));
        }
        let replayed = Self::from_bytes(&data)?;
        for root in self.inner.roots()? {
            for node in self.inner.walk(&root)? {
                if !self.staged.contains(&node.id) && replayed.get(&node.id)? != node {
                    return Err(Error::Integrity(
                        "hashrope log differs from materialized nodes".into(),
                    ));
                }
            }
        }
        Ok(())
    }
    fn append(&mut self, bytes: Vec<u8>) {
        // An imported final record may be valid without a line terminator.
        // Preserve imported bytes until the first append, then separate records.
        if self
            .chunks
            .last()
            .and_then(|chunk| chunk.last())
            .is_some_and(|byte| !matches!(byte, b'\n' | b'\r'))
        {
            self.hash = extend_hash(self.hash, b"\n");
            self.length += 1;
            self.chunks.push(vec![b'\n']);
        }
        self.hash = extend_hash(self.hash, &bytes);
        self.length += bytes.len();
        self.chunks.push(bytes);
    }
    fn rewrite_log(&mut self) -> Result<()> {
        let mut chunks = Vec::new();
        for root in self.inner.roots()? {
            for node in self.inner.walk(&root)? {
                if !self.staged.contains(&node.id) {
                    chunks.push(put_record(&node)?);
                }
            }
        }
        self.chunks.clear();
        self.hash = 0;
        self.length = 0;
        for bytes in chunks {
            self.append(bytes);
        }
        Ok(())
    }
}
impl Store for HashRopeStore {
    fn operation_log(&self) -> Result<Vec<u8>> {
        Ok(self.to_bytes())
    }
    fn put(&mut self, node: Node) -> Result<()> {
        if self.staged.contains(&node.id) {
            return Err(Error::Integrity(
                "cannot append an in-flight identity before finalization".into(),
            ));
        }
        let record = put_record(&node)?;
        let revision = self.inner.revision();
        self.inner.put(node)?;
        if revision != self.inner.revision() {
            self.append(record);
        }
        Ok(())
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.inner.get(id)
    }
    fn exists(&self, id: &str) -> bool {
        self.inner.exists(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.inner.children(id)
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.inner.roots()
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        let record = record_bytes(&json!({"op":"meta","id":id,"patch":patch}))?;
        self.inner.update_meta(id, patch)?;
        if !self.staged.contains(id) {
            self.append(record);
        }
        Ok(())
    }
    fn apply_batch(&mut self, nodes: Vec<Node>, patches: Vec<(String, Value)>) -> Result<()> {
        let mut staged = self.clone();
        for node in nodes {
            staged.put(node)?;
        }
        for (id, patch) in patches {
            staged.update_meta(&id, patch)?;
        }
        *self = staged;
        Ok(())
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        let mut staged = self.clone();
        staged.inner.drop_nodes(ids)?;
        staged.staged.retain(|id| !ids.contains(id));
        staged.rewrite_log()?;
        *self = staged;
        Ok(())
    }
    fn compact(&mut self) -> Result<usize> {
        self.rewrite_log()?;
        Ok(0)
    }
}
impl RecordingStore for HashRopeStore {
    fn stage_pending(&mut self, node: Node) -> Result<()> {
        if self.inner.exists(&node.id)
            || node.result.is_some()
            || node.meta.get("state").and_then(Value::as_str) != Some("pending")
        {
            return Err(Error::Integrity(
                "hashrope stage requires a new pending identity".into(),
            ));
        }
        let id = node.id.clone();
        self.inner.put(node)?;
        self.staged.insert(id);
        Ok(())
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        if !self.staged.contains(&node.id) {
            return Err(Error::Integrity("hashrope identity was not staged".into()));
        }
        let bytes = put_record(&node)?;
        let id = node.id.clone();
        self.inner.finalize(node)?;
        self.staged.remove(&id);
        self.append(bytes);
        Ok(())
    }
    fn revision(&self) -> Option<u64> {
        self.inner.revision()
    }
    fn trusted_immutable_identity(&self) -> bool {
        true
    }
}
