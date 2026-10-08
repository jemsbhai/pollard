//! Verified subtree interchange, conflict-aware merge and explicit offline GC.
use crate::seal::{seal, seal_nodes, SealReport};
use crate::sqlite::same_identity;
use crate::{canonical_bytes, Error, Node, NodeKind, Result, Store};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeReport {
    pub copied: usize,
    pub existing: usize,
    pub result_conflicts: usize,
    pub meta_conflicts: usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExportReport {
    pub path: String,
    pub root_id: String,
    pub digest: String,
    pub nodes: usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportReport {
    pub path: String,
    pub root_id: String,
    pub digest: String,
    pub imported: usize,
    pub existing: usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GCReport {
    pub mode: String,
    pub removed_nodes: usize,
    pub removed_node_ids: Vec<String>,
    pub removed_blobs: usize,
    pub survivor_seals: BTreeMap<String, String>,
}

#[derive(Serialize, Deserialize)]
struct Record {
    id: String,
    parent: Option<String>,
    kind: NodeKind,
    attempt: u64,
    payload: String,
    result: Option<String>,
    result_digest: Option<String>,
    meta: String,
}
impl Record {
    fn from_node(node: &Node) -> Result<Self> {
        node.validate()?;
        Ok(Self {
            id: node.id.clone(),
            parent: node.parent.clone(),
            kind: node.kind,
            attempt: node.attempt,
            payload: String::from_utf8(canonical_bytes(&node.payload)?)
                .map_err(|e| Error::Invalid(e.to_string()))?,
            result: node.result_text.clone(),
            result_digest: node.result_digest.clone(),
            meta: node.meta.to_string(),
        })
    }
    fn into_node(self) -> Result<Node> {
        Node::from_storage(
            self.id,
            self.parent,
            self.kind,
            self.attempt,
            &self.payload,
            self.result,
            self.result_digest,
            &self.meta,
        )
    }
}
#[derive(Serialize, Deserialize)]
struct Manifest {
    format: String,
    root_id: String,
    seal: SealReport,
    nodes: Vec<Record>,
}

/// Return the same `pollard/subtree/v1` document Python exports, retaining exact result text.
pub fn export_manifest<S: Store + ?Sized>(store: &S, root_id: &str) -> Result<Value> {
    let nodes = store.walk(root_id)?;
    validate_order(root_id, &nodes)?;
    let report = seal_nodes(root_id, &nodes)?;
    let records = nodes
        .iter()
        .map(Record::from_node)
        .collect::<Result<Vec<_>>>()?;
    serde_json::to_value(Manifest {
        format: "pollard/subtree/v1".into(),
        root_id: root_id.into(),
        seal: report,
        nodes: records,
    })
    .map_err(|e| Error::Invalid(e.to_string()))
}
pub fn export_subtree<S: Store + ?Sized>(
    store: &S,
    root_id: &str,
    path: impl AsRef<Path>,
) -> Result<ExportReport> {
    let manifest = export_manifest(store, root_id)?;
    let path = path.as_ref();
    let text =
        serde_json::to_string_pretty(&manifest).map_err(|e| Error::Invalid(e.to_string()))?;
    std::fs::write(path, format!("{text}\n"))
        .map_err(|e| Error::Invalid(format!("export: {e}")))?;
    Ok(ExportReport {
        path: path.to_string_lossy().into_owned(),
        root_id: root_id.into(),
        digest: manifest["seal"]["digest"]
            .as_str()
            .expect("serialized seal")
            .into(),
        nodes: manifest["nodes"]
            .as_array()
            .expect("serialized nodes")
            .len(),
    })
}
/// Fully validate a document and all destination collisions before applying it.
/// MemoryStore and SQLiteStore apply the resulting batch atomically.
pub fn import_manifest<S: Store + ?Sized>(document: Value, store: &mut S) -> Result<ImportReport> {
    let manifest: Manifest = serde_json::from_value(document)
        .map_err(|e| Error::Integrity(format!("invalid subtree manifest: {e}")))?;
    if manifest.format != "pollard/subtree/v1" {
        return Err(Error::Integrity("unsupported subtree export format".into()));
    }
    let nodes = manifest
        .nodes
        .into_iter()
        .map(Record::into_node)
        .collect::<Result<Vec<_>>>()?;
    validate_order(&manifest.root_id, &nodes)?;
    let actual = seal_nodes(&manifest.root_id, &nodes)?;
    if actual != manifest.seal {
        return Err(Error::Integrity(
            "subtree seal does not match manifest".into(),
        ));
    }
    if let Some(parent) = &nodes[0].parent {
        if !store.try_exists(parent)? {
            return Err(Error::Integrity(
                "subtree parent missing from destination".into(),
            ));
        }
    }
    let (imported, existing) = store.import_nodes(nodes)?;
    Ok(ImportReport {
        path: String::new(),
        root_id: manifest.root_id,
        digest: actual.digest,
        imported,
        existing,
    })
}
pub fn import_subtree<S: Store + ?Sized>(
    path: impl AsRef<Path>,
    store: &mut S,
) -> Result<ImportReport> {
    let text = std::fs::read_to_string(path.as_ref())
        .map_err(|e| Error::Invalid(format!("import: {e}")))?;
    let value = serde_json::from_str(&text)
        .map_err(|e| Error::Integrity(format!("invalid manifest JSON: {e}")))?;
    let mut report = import_manifest(value, store)?;
    report.path = path.as_ref().to_string_lossy().into_owned();
    Ok(report)
}

/// Union all roots after fully traversing and validating the source.
/// Replay merge rejects result collisions. Ordinary merge retains first results.
pub fn merge<D: Store + ?Sized, S: Store + ?Sized>(
    destination: &mut D,
    source: &S,
    replay: bool,
) -> Result<MergeReport> {
    let mut all = Vec::new();
    let mut seen = BTreeSet::new();
    for root in source.roots()? {
        let nodes = source.walk(&root)?;
        validate_order(&root, &nodes)?;
        if nodes[0].parent.is_some() {
            return Err(Error::Integrity("merge source root has a parent".into()));
        }
        for node in nodes {
            if !seen.insert(node.id.clone()) {
                return Err(Error::Integrity(
                    "merge source contains duplicate nodes".into(),
                ));
            }
            all.push(node);
        }
    }
    destination.merge_nodes(all, replay)
}

pub(crate) fn import_prepared<S: Store + ?Sized>(
    store: &mut S,
    nodes: Vec<Node>,
) -> Result<(usize, usize)> {
    let mut existing = 0;
    let mut incoming = Vec::new();
    for node in nodes {
        node.validate()?;
        if store.try_exists(&node.id)? {
            let old = store.get(&node.id)?;
            old.validate()?;
            if !same_identity(&old, &node)
                || old.result_text != node.result_text
                || old.result_digest != node.result_digest
            {
                return Err(Error::Integrity(format!(
                    "destination conflicts with imported node {}",
                    node.id
                )));
            }
            existing += 1;
        } else {
            incoming.push(node);
        }
    }
    let imported = incoming.len();
    store.apply_batch(incoming, Vec::new())?;
    Ok((imported, existing))
}

pub(crate) fn merge_prepared<D: Store + ?Sized>(
    destination: &mut D,
    all: Vec<Node>,
    replay: bool,
) -> Result<MergeReport> {
    let mut report = MergeReport::default();
    let mut added = Vec::new();
    let mut patches = Vec::new();
    for incoming in all {
        incoming.validate()?;
        if !destination.try_exists(&incoming.id)? {
            added.push(incoming);
            report.copied += 1;
            continue;
        }
        let existing = destination.get(&incoming.id)?;
        existing.validate()?;
        if !same_identity(&existing, &incoming) {
            return Err(Error::Integrity(
                "node identity collision during merge".into(),
            ));
        }
        let result_conflict =
            incoming.result_text.is_some() && incoming.result_text != existing.result_text;
        if replay && result_conflict {
            return Err(Error::Integrity(format!(
                "result collision during replay merge: {}",
                incoming.id
            )));
        }
        report.existing += 1;
        let (mut meta, count) = merge_meta(&existing.meta, &incoming.meta);
        report.meta_conflicts += count;
        if result_conflict {
            let old = list(meta.get("result_conflicts"));
            let updated = union(
                &old,
                &[json!({"result_digest":incoming.result_digest,"result":incoming.result})],
            );
            if updated.len() > old.len() {
                report.result_conflicts += 1;
            }
            meta["result_conflicts"] = json!(updated);
        }
        if meta != existing.meta {
            patches.push((existing.id, meta));
        }
    }
    destination.apply_batch(added, patches)?;
    Ok(report)
}

/// Run only while writers are stopped; `compact` removes unreferenced blobs.
pub fn gc<S: Store + ?Sized>(store: &mut S, mode: &str) -> Result<GCReport> {
    if !["drop-pruned", "compact"].contains(&mode) {
        return Err(Error::Invalid("unsupported garbage collection mode".into()));
    }
    let roots = store.roots()?;
    for root in &roots {
        seal(store, root)?;
    }
    let mut removed = BTreeSet::new();
    let removed_blobs = if mode == "drop-pruned" {
        for root in roots {
            for node in store.walk(&root)? {
                if !removed.contains(&node.id)
                    && node.meta.get("pruned") == Some(&Value::Bool(true))
                {
                    removed.extend(store.walk(&node.id)?.into_iter().map(|n| n.id));
                }
            }
        }
        store.drop_nodes(&removed)?;
        0
    } else {
        store.compact()?
    };
    let mut survivors = BTreeMap::new();
    for root in store.roots()? {
        survivors.insert(root.clone(), seal(store, &root)?.digest);
    }
    Ok(GCReport {
        mode: mode.into(),
        removed_nodes: removed.len(),
        removed_node_ids: removed.into_iter().collect(),
        removed_blobs,
        survivor_seals: survivors,
    })
}

fn validate_order(root: &str, nodes: &[Node]) -> Result<()> {
    if nodes.first().map(|n| n.id.as_str()) != Some(root) {
        return Err(Error::Integrity(
            "subtree traversal does not start at declared root".into(),
        ));
    }
    let mut seen = BTreeSet::new();
    let mut children: BTreeMap<&str, Vec<&Node>> = BTreeMap::new();
    for (index, node) in nodes.iter().enumerate() {
        node.validate()?;
        if !seen.insert(node.id.as_str()) {
            return Err(Error::Integrity("duplicate subtree node".into()));
        }
        if index > 0 && !node.parent.as_deref().is_some_and(|p| seen.contains(p)) {
            return Err(Error::Integrity(
                "subtree parent missing or out of order".into(),
            ));
        }
        if let Some(parent) = node.parent.as_deref() {
            children.entry(parent).or_default().push(node);
        }
    }
    if nodes[0].parent.as_deref().is_some_and(|p| seen.contains(p)) {
        return Err(Error::Integrity(
            "subtree root parent inside manifest".into(),
        ));
    }
    for siblings in children.values_mut() {
        siblings.sort_by_key(|n| (n.kind.as_str(), n.id.as_str()));
    }
    let mut pending = vec![root];
    let mut walked = Vec::new();
    while let Some(id) = pending.pop() {
        walked.push(id);
        if let Some(children) = children.get(id) {
            pending.extend(children.iter().rev().map(|n| n.id.as_str()));
        }
    }
    if walked != nodes.iter().map(|n| n.id.as_str()).collect::<Vec<_>>() {
        return Err(Error::Integrity(
            "subtree nodes not in deterministic walk order".into(),
        ));
    }
    Ok(())
}
fn list(value: Option<&Value>) -> Vec<Value> {
    value.and_then(Value::as_array).cloned().unwrap_or_default()
}
fn union(a: &[Value], b: &[Value]) -> Vec<Value> {
    let values: BTreeMap<_, _> = a
        .iter()
        .chain(b.iter())
        .map(|v| {
            (
                crate::result_text_and_digest(v)
                    .expect("valid JSON metadata")
                    .0,
                v.clone(),
            )
        })
        .collect();
    values.into_values().collect()
}
fn merge_meta(existing: &Value, incoming: &Value) -> (Value, usize) {
    let mut merged = existing.clone();
    let mut conflicts = Vec::new();
    let recorded = union(
        &list(existing.get("merge_conflicts")),
        &list(incoming.get("merge_conflicts")),
    );
    for (key, value) in incoming.as_object().expect("validated meta") {
        if key == "merge_conflicts" {
            continue;
        }
        if let Some(old) = merged.get(key) {
            let (value, found) = merge_value(old, value, key);
            merged[key] = value;
            conflicts.extend(found);
        } else {
            merged[key] = value.clone();
        }
    }
    let updated = union(&recorded, &conflicts);
    let count = updated.len() - recorded.len();
    if !updated.is_empty() {
        merged["merge_conflicts"] = json!(updated);
    }
    (merged, count)
}
fn merge_value(existing: &Value, incoming: &Value, path: &str) -> (Value, Vec<Value>) {
    if existing == incoming {
        return (existing.clone(), Vec::new());
    }
    if existing.is_object() && incoming.is_object() {
        let mut merged = existing.clone();
        let mut conflicts = Vec::new();
        for (key, value) in incoming.as_object().expect("object") {
            if let Some(old) = merged.get(key) {
                let (value, found) = merge_value(old, value, &format!("{path}.{key}"));
                merged[key] = value;
                conflicts.extend(found);
            } else {
                merged[key] = value.clone();
            }
        }
        return (merged, conflicts);
    }
    if let (Some(a), Some(b)) = (existing.as_array(), incoming.as_array()) {
        return (json!(union(a, b)), Vec::new());
    }
    (
        existing.clone(),
        vec![
            json!({"path":path,"values":union(std::slice::from_ref(existing),std::slice::from_ref(incoming))}),
        ],
    )
}
