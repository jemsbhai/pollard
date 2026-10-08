//! Deterministic, Python-compatible rolling seals over subtree records.
use crate::identity::hash;
use crate::{canonical_bytes, Node, Result, Store};
use serde::{Deserialize, Serialize};
use serde_json::json;

pub const SEAL_ALGORITHM: &str = "sha256:pollard/v1:seal";
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SealEntry {
    pub index: usize,
    pub node_id: String,
    pub parent_id: Option<String>,
    pub kind: String,
    pub result_digest: Option<String>,
    pub previous: Option<String>,
    pub seal: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SealReport {
    pub root_id: String,
    pub algorithm: String,
    pub digest: String,
    pub entries: Vec<SealEntry>,
}
pub fn seal<S: Store + ?Sized>(store: &S, root_id: &str) -> Result<SealReport> {
    seal_nodes(root_id, &store.walk(root_id)?)
}
pub(crate) fn seal_nodes(root_id: &str, nodes: &[Node]) -> Result<SealReport> {
    let mut previous = String::new();
    let mut entries = Vec::with_capacity(nodes.len());
    for (index, node) in nodes.iter().enumerate() {
        node.validate()?;
        let record = json!({"index":index,"node_id":node.id,"parent_id":node.parent.as_deref().unwrap_or(""),"kind":node.kind.as_str(),"result_digest":node.result_digest.as_deref().unwrap_or(""),"previous":previous});
        let digest = hash(b"pollard/v1:seal\n", &canonical_bytes(&record)?);
        entries.push(SealEntry {
            index,
            node_id: node.id.clone(),
            parent_id: node.parent.clone(),
            kind: node.kind.as_str().into(),
            result_digest: node.result_digest.clone(),
            previous: if previous.is_empty() {
                None
            } else {
                Some(previous)
            },
            seal: digest.clone(),
        });
        previous = digest;
    }
    Ok(SealReport {
        root_id: root_id.into(),
        algorithm: SEAL_ALGORITHM.into(),
        digest: previous,
        entries,
    })
}
