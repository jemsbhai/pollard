use crate::identity::hex64;
use crate::{
    canonical_bytes, node_id, result_digest_from_text, result_text_and_digest, Error, Result,
};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

pub type LeaseRenewer = Arc<dyn Fn(&str, f64) -> Result<bool> + Send + Sync>;

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
#[derive(Debug, Clone, Serialize, Deserialize)]
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

impl PartialEq for Node {
    fn eq(&self, other: &Self) -> bool {
        self.id == other.id
            && self.parent == other.parent
            && self.kind == other.kind
            && self.attempt == other.attempt
            && self.payload == other.payload
            && self.result_text == other.result_text
            && self.result_digest == other.result_digest
            && match (&self.result, &other.result) {
                (None, None) => true,
                (Some(left), Some(right)) => crate::identity::result_values_equal(left, right),
                _ => false,
            }
            && crate::identity::result_values_equal(&self.meta, &other.meta)
    }
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
        crate::identity::validate_finite_json(&self.meta)?;
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
                if !crate::identity::result_values_equal(result, &parsed) {
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

/// A store-local cache token distinguishing local writes from independent ones.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StoreRevision {
    /// Mutations through this store instance.
    pub local: u64,
    /// All independently committed mutations, including identities and blobs.
    pub external: u64,
}

/// Optional settlement extension required by live runtimes. Finalization must
/// atomically replace only a pending, resultless node with the same identity.
pub trait RecordingStore: Store {
    fn finalize(&mut self, node: Node) -> Result<()>;
    /// Stage a live call before dispatch. Append-only backends may keep this
    /// transient and publish a single completed record during finalization.
    fn stage_pending(&mut self, node: Node) -> Result<()> {
        self.put(node)
    }
    /// Revision covering every store mutation, or None for externally writable stores.
    fn revision(&self) -> Option<u64> {
        None
    }
    /// Existing settled identity/result bytes cannot change through this backend.
    fn trusted_immutable_identity(&self) -> bool {
        false
    }
    /// A token valid only on this store instance. Returning Some promises that
    /// every committed mutation changes a component and that reads observe the
    /// current token. Local writes preserve settled identity/result bytes; other
    /// changes, including external SQL and triggers, must change `external`.
    /// A single-node write must not modify other cached nodes; such side effects
    /// require changing `external` or returning None instead.
    /// Runtimes recheck tokens after scans and discard caches on any change.
    fn cache_revision(&self) -> Option<StoreRevision> {
        if self.trusted_immutable_identity() {
            self.revision()
                .map(|local| StoreRevision { local, external: 0 })
        } else {
            None
        }
    }
    fn supports_reservations(&self) -> bool {
        false
    }
    /// None means this backend has no shared-budget arbiter.
    fn reserve_budget(
        &mut self,
        _id: &str,
        _budgets: &[crate::BudgetReservation],
        _windows: &[crate::WindowReservation],
        _lease_seconds: f64,
    ) -> Result<Option<crate::ReservationCheck>> {
        Ok(None)
    }
    fn settle_budget(&mut self, _id: &str, _charges: &BTreeMap<String, Decimal>) -> Result<()> {
        Err(Error::Invalid("store has no shared-budget arbiter".into()))
    }
    fn release_budget(&mut self, _id: &str) -> Result<()> {
        Err(Error::Invalid("store has no shared-budget arbiter".into()))
    }
    /// An independent connection-safe heartbeat, callable while a handler executes.
    fn lease_renewer(&self) -> Option<LeaseRenewer> {
        None
    }
}

/// Backends return detached owned records. Mutations occur only via methods.
pub trait Store {
    /// Snapshot compatible append-only log bytes, when the backend exposes one.
    fn operation_log(&self) -> Result<Vec<u8>> {
        Err(Error::Invalid(
            "backend does not expose an operation log".into(),
        ))
    }
    fn put(&mut self, node: Node) -> Result<()>;
    fn get(&self, id: &str) -> Result<Node>;
    fn exists(&self, id: &str) -> bool;
    /// Fallible existence query. Runtime dispatch must use this method so a
    /// remote connection failure cannot be mistaken for a missing recording.
    fn try_exists(&self, id: &str) -> Result<bool> {
        Ok(self.exists(id))
    }
    fn children(&self, id: &str) -> Result<Vec<String>>;
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()>;
    fn roots(&self) -> Result<Vec<String>>;
    /// Import collision checks and writes share one transaction on native stores.
    fn import_nodes(&mut self, nodes: Vec<Node>) -> Result<(usize, usize)> {
        crate::merge::import_prepared(self, nodes)
    }
    /// Merge destination reads and writes share one transaction on SQLite.
    fn merge_nodes(&mut self, nodes: Vec<Node>, replay: bool) -> Result<crate::MergeReport> {
        crate::merge::merge_prepared(self, nodes, replay)
    }
    /// Apply a prevalidated batch. Backends may override to provide atomicity.
    fn apply_batch(&mut self, nodes: Vec<Node>, patches: Vec<(String, Value)>) -> Result<()> {
        for node in nodes {
            self.put(node)?;
        }
        for (id, patch) in patches {
            self.update_meta(&id, patch)?;
        }
        Ok(())
    }
    fn drop_nodes(&mut self, _ids: &BTreeSet<String>) -> Result<()> {
        Err(Error::Invalid(
            "backend does not support garbage collection".into(),
        ))
    }
    fn compact(&mut self) -> Result<usize> {
        Err(Error::Invalid("backend does not support compaction".into()))
    }
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
    children: BTreeMap<String, BTreeSet<(String, String)>>,
    revision: u64,
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
                self.revision = self.revision.wrapping_add(1);
            }
            return Ok(());
        }
        if node.meta.get("state").and_then(Value::as_str) == Some("pending")
            && node.result_text.is_none()
        {
            self.pending.insert(node.id.clone());
        }
        if let Some(parent) = &node.parent {
            self.children
                .entry(parent.clone())
                .or_default()
                .insert((node.kind.as_str().to_owned(), node.id.clone()));
        }
        self.nodes.insert(node.id.clone(), node);
        self.revision = self.revision.wrapping_add(1);
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
        Ok(self
            .children
            .get(id)
            .into_iter()
            .flatten()
            .map(|(_, id)| id.clone())
            .collect())
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
        self.revision = self.revision.wrapping_add(1);
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
        if self
            .nodes
            .values()
            .any(|n| !ids.contains(&n.id) && n.parent.as_ref().is_some_and(|p| ids.contains(p)))
        {
            return Err(Error::Integrity(
                "garbage collection would orphan a child".into(),
            ));
        }
        self.nodes.retain(|id, _| !ids.contains(id));
        self.pending.retain(|id| !ids.contains(id));
        self.children.retain(|id, _| !ids.contains(id));
        for children in self.children.values_mut() {
            children.retain(|(_, id)| !ids.contains(id));
        }
        self.revision = self.revision.wrapping_add(1);
        Ok(())
    }
    fn compact(&mut self) -> Result<usize> {
        Ok(0)
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
        self.revision = self.revision.wrapping_add(1);
        Ok(())
    }
    fn revision(&self) -> Option<u64> {
        Some(self.revision)
    }
    fn trusted_immutable_identity(&self) -> bool {
        true
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

/// Verify a subtree and its external ancestry, including child-index consistency.
/// The Python-compatible `verify` function intentionally checks ancestry only.
pub fn verify_subtree<S: Store + ?Sized>(store: &S, root: &str) -> VerifyReport {
    let mut report = verify(store, root);
    let mut pending = vec![(root.to_owned(), None)];
    let mut seen = BTreeSet::new();
    while let Some((id, expected_parent)) = pending.pop() {
        if !seen.insert(id.clone()) {
            report.findings.push(VerifyFinding {
                node_id: id,
                message: "cycle or duplicate in subtree".into(),
            });
            continue;
        }
        match store.get(&id) {
            Ok(node) => {
                if node.id != id
                    || expected_parent
                        .as_ref()
                        .is_some_and(|p| node.parent.as_ref() != Some(p))
                {
                    report.findings.push(VerifyFinding {
                        node_id: id.clone(),
                        message: "child index disagrees with stored identity".into(),
                    });
                }
                if id != root {
                    if let Err(e) = node.validate() {
                        report.findings.push(VerifyFinding {
                            node_id: id.clone(),
                            message: e.to_string(),
                        });
                    }
                }
                match store.children(&id) {
                    Ok(children) => pending.extend(
                        children
                            .into_iter()
                            .rev()
                            .map(|child| (child, Some(id.clone()))),
                    ),
                    Err(e) => report.findings.push(VerifyFinding {
                        node_id: id,
                        message: e.to_string(),
                    }),
                }
            }
            Err(e) => report.findings.push(VerifyFinding {
                node_id: id,
                message: e.to_string(),
            }),
        }
    }
    report.ok = report.findings.is_empty();
    report
}
