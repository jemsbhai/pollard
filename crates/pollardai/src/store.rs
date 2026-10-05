use crate::identity::hex64;
use crate::{
    canonical_bytes, node_id, result_digest_from_text, result_text_and_digest, Error, Result,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeKind {
    Root,
    ModelCall,
    ToolCall,
    Note,
    Refusal,
}

impl NodeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Root => "root",
            Self::ModelCall => "model_call",
            Self::ToolCall => "tool_call",
            Self::Note => "note",
            Self::Refusal => "refusal",
        }
    }
}

/// Detached owned record. Result integrity is checked against exact result text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Node {
    pub id: String,
    pub parent: Option<String>,
    pub kind: NodeKind,
    pub attempt: u64,
    pub payload: Value,
    pub result: Option<Value>,
    pub result_text: Option<String>,
    pub result_digest: Option<String>,
    pub meta: Value,
}

impl Node {
    pub fn make(
        kind: NodeKind,
        parent: Option<&str>,
        attempt: u64,
        payload: Value,
        result: Option<Value>,
        meta: Value,
    ) -> Result<Self> {
        let (result_text, result_digest) = match &result {
            Some(v) => {
                let (t, d) = result_text_and_digest(v)?;
                (Some(t), Some(d))
            }
            None => (None, None),
        };
        let node = Self {
            id: node_id(kind.as_str(), parent, attempt, &payload)?,
            parent: parent.map(str::to_owned),
            kind,
            attempt,
            payload,
            result,
            result_text,
            result_digest,
            meta,
        };
        node.validate()?;
        Ok(node)
    }

    /// Import exact stored text without changing its digest surface.
    #[allow(clippy::too_many_arguments)]
    pub fn from_storage(
        id: String,
        parent: Option<String>,
        kind: NodeKind,
        attempt: u64,
        payload_text: &str,
        result_text: Option<String>,
        result_digest: Option<String>,
        meta_text: &str,
    ) -> Result<Self> {
        let parse =
            |text: &str| serde_json::from_str(text).map_err(|e| Error::Invalid(e.to_string()));
        let result = result_text.as_deref().map(parse).transpose()?;
        let node = Self {
            id,
            parent,
            kind,
            attempt,
            payload: parse(payload_text)?,
            result,
            result_text,
            result_digest,
            meta: parse(meta_text)?,
        };
        node.validate_shape()?;
        Ok(node)
    }

    pub fn expected_id(&self) -> Result<String> {
        node_id(
            self.kind.as_str(),
            self.parent.as_deref(),
            self.attempt,
            &self.payload,
        )
    }

    fn validate_shape(&self) -> Result<()> {
        if !hex64(&self.id) {
            return Err(Error::Integrity(
                "node id must be 64 lowercase hex characters".into(),
            ));
        }
        if self.kind == NodeKind::Root {
            if self.parent.is_some() {
                return Err(Error::Integrity("root nodes cannot have parents".into()));
            }
        } else if !self.parent.as_deref().is_some_and(hex64) {
            return Err(Error::Integrity(
                "non-root nodes require a 64-hex parent".into(),
            ));
        }
        if !self.payload.is_object() || !self.meta.is_object() {
            return Err(Error::Invalid(
                "payload and meta must be JSON objects".into(),
            ));
        }
        canonical_bytes(&self.payload)?;
        crate::identity::safe_amount(self.attempt, "attempt")?;
        if self.result_digest.as_deref().is_some_and(|s| !hex64(s)) {
            return Err(Error::Integrity("invalid result digest".into()));
        }
        Ok(())
    }

    pub fn validate(&self) -> Result<()> {
        self.validate_shape()?;
        if self.id != self.expected_id()? {
            return Err(Error::Integrity(
                "node id does not match identity fields".into(),
            ));
        }
        match (&self.result, &self.result_text, &self.result_digest) {
            (None, None, None) => {}
            (Some(result), Some(text), Some(digest)) => {
                if *digest != result_digest_from_text(text) {
                    return Err(Error::Integrity(
                        "result digest does not match stored result".into(),
                    ));
                }
                let parsed: Value =
                    serde_json::from_str(text).map_err(|e| Error::Integrity(e.to_string()))?;
                if *result != parsed {
                    return Err(Error::Integrity(
                        "result differs from stored result text".into(),
                    ));
                }
            }
            _ => {
                return Err(Error::Integrity(
                    "result, result_text and result_digest must be present together".into(),
                ))
            }
        }
        Ok(())
    }
}

/// Optional settlement extension required by live runtimes. Finalization must
/// atomically replace only a pending, resultless node with the same identity.
pub trait RecordingStore: Store {
    fn finalize(&mut self, node: Node) -> Result<()>;
}

/// Backends return detached owned records. Mutations occur only via methods.
pub trait Store {
    fn put(&mut self, node: Node) -> Result<()>;
    fn get(&self, id: &str) -> Result<Node>;
    fn exists(&self, id: &str) -> bool;
    fn children(&self, id: &str) -> Result<Vec<String>>;
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()>;
    fn roots(&self) -> Result<Vec<String>>;
    fn walk(&self, root: &str) -> Result<Vec<Node>> {
        let mut pending = vec![root.to_owned()];
        let mut seen = BTreeSet::new();
        let mut nodes = Vec::new();
        while let Some(id) = pending.pop() {
            if !seen.insert(id.clone()) {
                return Err(Error::Integrity(
                    "cycle or duplicate in store traversal".into(),
                ));
            }
            nodes.push(self.get(&id)?);
            pending.extend(self.children(&id)?.into_iter().rev());
        }
        Ok(nodes)
    }
}

#[derive(Debug, Clone, Default)]
pub struct MemoryStore {
    nodes: BTreeMap<String, Node>,
    pending: BTreeSet<String>,
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Store for MemoryStore {
    fn put(&mut self, node: Node) -> Result<()> {
        node.validate()?;
        if let Some(parent) = &node.parent {
            if !self.nodes.contains_key(parent) {
                return Err(Error::NotFound(parent.clone()));
            }
        }
        if let Some(existing) = self.nodes.get_mut(&node.id) {
            if existing.parent != node.parent
                || existing.kind != node.kind
                || existing.attempt != node.attempt
                || existing.payload != node.payload
            {
                return Err(Error::Integrity("node identity collision".into()));
            }
            if node.result_text.is_some() && node.result_text != existing.result_text {
                let conflicts = existing
                    .meta
                    .as_object_mut()
                    .expect("validated meta")
                    .entry("result_conflicts")
                    .or_insert_with(|| json!([]));
                conflicts
                    .as_array_mut()
                    .ok_or_else(|| Error::Integrity("invalid result_conflicts metadata".into()))?
                    .push(json!({"result_digest":node.result_digest,"result":node.result}));
            }
            return Ok(());
        }
        if node.meta.get("state").and_then(Value::as_str) == Some("pending")
            && node.result_text.is_none()
        {
            self.pending.insert(node.id.clone());
        }
        self.nodes.insert(node.id.clone(), node);
        Ok(())
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.nodes
            .get(id)
            .cloned()
            .ok_or_else(|| Error::NotFound(id.to_owned()))
    }
    fn exists(&self, id: &str) -> bool {
        self.nodes.contains_key(id)
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        let mut nodes: Vec<_> = self
            .nodes
            .values()
            .filter(|n| n.parent.as_deref() == Some(id))
            .collect();
        nodes.sort_by_key(|n| (n.kind.as_str(), n.id.as_str()));
        Ok(nodes.into_iter().map(|n| n.id.clone()).collect())
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        let patch = patch
            .as_object()
            .ok_or_else(|| Error::Invalid("meta patch must be object".into()))?;
        let node = self
            .nodes
            .get_mut(id)
            .ok_or_else(|| Error::NotFound(id.to_owned()))?;
        node.meta
            .as_object_mut()
            .expect("validated meta")
            .extend(patch.clone());
        Ok(())
    }
    fn roots(&self) -> Result<Vec<String>> {
        let mut roots: Vec<_> = self.nodes.values().filter(|n| n.parent.is_none()).collect();
        roots.sort_by_key(|n| {
            (
                n.payload.get("run").and_then(Value::as_str).unwrap_or(""),
                n.id.as_str(),
            )
        });
        Ok(roots.into_iter().map(|n| n.id.clone()).collect())
    }
}

impl RecordingStore for MemoryStore {
    fn finalize(&mut self, node: Node) -> Result<()> {
        node.validate()?;
        let old = self
            .nodes
            .get(&node.id)
            .ok_or_else(|| Error::NotFound(node.id.clone()))?;
        if !self.pending.contains(&node.id)
            || old.meta.get("state").and_then(Value::as_str) != Some("pending")
            || old.result_text.is_some()
            || old.parent != node.parent
            || old.kind != node.kind
            || old.attempt != node.attempt
            || old.payload != node.payload
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
        self.pending.remove(&node.id);
        self.nodes.insert(node.id.clone(), node);
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyFinding {
    pub node_id: String,
    pub message: String,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyReport {
    pub ok: bool,
    pub findings: Vec<VerifyFinding>,
}

/// Verify a node and all ancestors, including lookup keys and stored result text.
pub fn verify<S: Store + ?Sized>(store: &S, id: &str) -> VerifyReport {
    let mut findings = Vec::new();
    let mut seen = BTreeSet::new();
    let mut current = Some(id.to_owned());
    while let Some(id) = current {
        if !seen.insert(id.clone()) {
            findings.push(VerifyFinding {
                node_id: id,
                message: "cycle detected in ancestry".into(),
            });
            break;
        }
        match store.get(&id) {
            Ok(node) => {
                if node.id != id {
                    findings.push(VerifyFinding {
                        node_id: id.clone(),
                        message: "lookup key differs from node id".into(),
                    });
                }
                if let Err(e) = node.validate() {
                    findings.push(VerifyFinding {
                        node_id: id,
                        message: e.to_string(),
                    });
                }
                current = node.parent;
            }
            Err(e) => {
                findings.push(VerifyFinding {
                    node_id: id,
                    message: e.to_string(),
                });
                break;
            }
        }
    }
    VerifyReport {
        ok: findings.is_empty(),
        findings,
    }
}
