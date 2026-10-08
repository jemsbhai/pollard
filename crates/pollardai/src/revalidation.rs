//! Explicit execution fingerprints and value-free model-result comparisons.
use crate::identity::{compare_integers, integer_text};
use crate::{canonical_bytes, json, Error, Node, Result, Value};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const REPLAY_CONTRACT_FORMAT: &str = "pollard/replay-contract/v1";
pub const REVALIDATION_FORMAT: &str = "pollard/revalidation/v1";
pub const MAX_DIFFERENCE_PATHS: usize = 100;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayContract {
    pub provider: String,
    pub model_revision: Option<String>,
    pub api_version: Option<String>,
    pub adapter: Option<String>,
    pub adapter_version: Option<String>,
    pub sdk: Option<String>,
    pub sdk_version: Option<String>,
    pub application_revision: Option<String>,
    pub environment: Value,
}
impl ReplayContract {
    pub fn new(provider: impl Into<String>) -> Result<Self> {
        let contract = Self {
            provider: provider.into(),
            model_revision: None,
            api_version: None,
            adapter: None,
            adapter_version: None,
            sdk: None,
            sdk_version: None,
            application_revision: None,
            environment: json!({}),
        };
        contract.validate()?;
        Ok(contract)
    }
    pub fn validate(&self) -> Result<()> {
        require_nonempty("provider", &self.provider)?;
        for (name, value) in self.optional_fields() {
            if let Some(value) = value {
                require_nonempty(name, value)?;
            }
        }
        if !self.environment.is_object() {
            return Err(Error::Invalid("environment must be an object".into()));
        }
        canonical_bytes(&self.environment)?;
        Ok(())
    }
    fn optional_fields(&self) -> [(&str, Option<&str>); 7] {
        [
            ("model_revision", self.model_revision.as_deref()),
            ("api_version", self.api_version.as_deref()),
            ("adapter", self.adapter.as_deref()),
            ("adapter_version", self.adapter_version.as_deref()),
            ("sdk", self.sdk.as_deref()),
            ("sdk_version", self.sdk_version.as_deref()),
            ("application_revision", self.application_revision.as_deref()),
        ]
    }
    pub fn to_value(&self) -> Value {
        let mut result = json!({"format": REPLAY_CONTRACT_FORMAT, "provider": self.provider});
        for (name, value) in self.optional_fields() {
            if let Some(value) = value {
                result[name] = json!(value);
            }
        }
        if self
            .environment
            .as_object()
            .is_some_and(|obj| !obj.is_empty())
        {
            result["environment"] = self.environment.clone();
        }
        result
    }
    pub fn bind(&self, payload: Value) -> Result<Value> {
        self.validate()?;
        canonical_bytes(&payload)?;
        let mut bound = require_object(payload, "payload")?;
        let mut metadata = metadata(&bound)?;
        let contract = self.to_value();
        if metadata
            .get("replay_contract")
            .is_some_and(|existing| !existing.is_null() && !python_equal(existing, &contract))
        {
            return Err(Error::Invalid(
                "payload is already bound to a different replay contract".into(),
            ));
        }
        metadata.insert("replay_contract".into(), contract);
        bound.insert("_pollard".into(), Value::Object(metadata));
        Ok(Value::Object(bound))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RevalidationComparison {
    pub matched: bool,
    pub difference_paths: Vec<String>,
    pub truncated: bool,
}
impl RevalidationComparison {
    pub fn new(matched: bool, difference_paths: Vec<String>, truncated: bool) -> Result<Self> {
        let comparison = Self {
            matched,
            difference_paths,
            truncated,
        };
        comparison.validate()?;
        Ok(comparison)
    }
    pub fn validate(&self) -> Result<()> {
        if self.difference_paths.len() > MAX_DIFFERENCE_PATHS {
            return Err(Error::Invalid(format!(
                "difference paths cannot exceed {MAX_DIFFERENCE_PATHS} entries"
            )));
        }
        if self
            .difference_paths
            .iter()
            .any(|path| !path.starts_with('/'))
        {
            return Err(Error::Invalid(
                "difference paths must be JSON pointers".into(),
            ));
        }
        if self.matched && (!self.difference_paths.is_empty() || self.truncated) {
            return Err(Error::Invalid(
                "a matched comparison cannot contain differences".into(),
            ));
        }
        Ok(())
    }
    pub fn to_value(&self) -> Value {
        json!({"matched":self.matched,"difference_paths":self.difference_paths,"truncated":self.truncated})
    }
}

pub trait RevalidationComparator: Send + Sync {
    fn name(&self) -> &str;
    fn compare(&self, recorded: &Value, live: &Value) -> Result<RevalidationComparison>;
}
#[derive(Debug, Clone, Copy, Default)]
pub struct ExactResultComparator;
impl RevalidationComparator for ExactResultComparator {
    fn name(&self) -> &str {
        "exact-result/v1"
    }
    fn compare(&self, recorded: &Value, live: &Value) -> Result<RevalidationComparison> {
        Ok(difference_paths(recorded, live))
    }
}
#[derive(Debug, Clone, Copy, Default)]
pub struct NormalizedModelComparator;
impl RevalidationComparator for NormalizedModelComparator {
    fn name(&self) -> &str {
        "normalized-model/v1"
    }
    fn compare(&self, recorded: &Value, live: &Value) -> Result<RevalidationComparison> {
        Ok(difference_paths(
            &model_semantics(recorded)?,
            &model_semantics(live)?,
        ))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RevalidationReport {
    pub observation_id: String,
    pub recorded_node_id: String,
    pub live_node_id: String,
    pub evidence_node_id: String,
    pub comparator: String,
    pub matched: bool,
    pub exact_match: bool,
    pub recorded_result_digest: String,
    pub live_result_digest: String,
    pub difference_paths: Vec<String>,
    pub differences_truncated: bool,
    pub recorded_contract: Option<Value>,
    pub live_contract: Value,
    pub charges: BTreeMap<String, f64>,
}
impl RevalidationReport {
    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).expect("revalidation report is JSON serializable")
    }
}

pub fn extract_replay_contract(payload: &Value) -> Result<Option<Value>> {
    let Some(contract) = payload
        .get("_pollard")
        .and_then(Value::as_object)
        .and_then(|obj| obj.get("replay_contract"))
    else {
        return Ok(None);
    };
    if !contract.is_object() {
        return Err(Error::Invalid(
            "recorded _pollard.replay_contract must be an object".into(),
        ));
    }
    canonical_bytes(contract)?;
    Ok(Some(contract.clone()))
}

pub fn make_revalidation_payload(
    payload: &Value,
    observation_id: &str,
    recorded: &Node,
    contract: &ReplayContract,
    comparator_name: &str,
) -> Result<Value> {
    require_nonempty("observation_id", observation_id)?;
    require_nonempty("comparator name", comparator_name)?;
    contract.validate()?;
    let mut marked = require_object(payload.clone(), "payload")?;
    let mut metadata = metadata(&marked)?;
    if metadata.contains_key("revalidation") {
        return Err(Error::Invalid(
            "payload already contains reserved _pollard.revalidation metadata".into(),
        ));
    }
    let digest = recorded
        .result_digest
        .as_ref()
        .ok_or_else(|| Error::Invalid("recorded result has no digest".into()))?;
    metadata.insert("revalidation".into(), json!({"format":REVALIDATION_FORMAT,"observation_id":observation_id,"recorded_node_id":recorded.id,"recorded_result_digest":digest,"live_contract":contract.to_value(),"comparator":comparator_name}));
    marked.insert("_pollard".into(), Value::Object(metadata));
    let marked = Value::Object(marked);
    canonical_bytes(&marked)?;
    Ok(marked)
}

fn require_nonempty(name: &str, value: &str) -> Result<()> {
    if value.trim().is_empty() {
        return Err(Error::Invalid(format!("{name} must be a non-empty string")));
    }
    Ok(())
}
fn require_object(value: Value, name: &str) -> Result<serde_json::Map<String, Value>> {
    if let Value::Object(obj) = value {
        Ok(obj)
    } else {
        Err(Error::Invalid(format!("{name} must be an object")))
    }
}
fn metadata(payload: &serde_json::Map<String, Value>) -> Result<serde_json::Map<String, Value>> {
    match payload.get("_pollard") {
        Some(Value::Object(obj)) => Ok(obj.clone()),
        None | Some(Value::Null) => Ok(serde_json::Map::new()),
        _ => Err(Error::Invalid(
            "payload _pollard field must be an object".into(),
        )),
    }
}
fn model_semantics(result: &Value) -> Result<Value> {
    let result = result
        .as_object()
        .ok_or_else(|| Error::Invalid("model comparison result must be an object".into()))?;
    let semantics = ["text", "tool_calls", "refusal", "structured_output"];
    let value = if semantics.iter().any(|key| result.contains_key(*key)) {
        semantics
            .iter()
            .filter_map(|key| {
                result.get(*key).map(|value| {
                    (
                        (*key).to_owned(),
                        if *key == "tool_calls" {
                            normalize_tool_calls(value)
                        } else {
                            value.clone()
                        },
                    )
                })
            })
            .collect()
    } else {
        result
            .iter()
            .filter(|(key, _)| !["usage", "provider_usage", "chunks"].contains(&key.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    };
    Ok(Value::Object(value))
}
fn normalize_tool_calls(value: &Value) -> Value {
    let Some(calls) = value.as_array() else {
        return value.clone();
    };
    Value::Array(
        calls
            .iter()
            .map(|call| {
                let Some(call) = call.as_object() else {
                    return call.clone();
                };
                let mut result = serde_json::Map::new();
                for (key, item) in call {
                    if ["id", "call_id", "toolUseId", "index"].contains(&key.as_str()) {
                        continue;
                    }
                    let mut item = item.clone();
                    if key == "function" && item.is_object() {
                        if let Some(arguments) = item.get("arguments").and_then(Value::as_str) {
                            item["arguments"] = serde_json::from_str(arguments)
                                .unwrap_or_else(|_| json!(arguments));
                        }
                    } else if ["arguments", "input_json"].contains(&key.as_str()) {
                        if let Some(arguments) = item.as_str() {
                            item = serde_json::from_str(arguments)
                                .unwrap_or_else(|_| json!(arguments));
                        }
                    }
                    result.insert(key.clone(), item);
                }
                Value::Object(result)
            })
            .collect(),
    )
}

fn python_type(value: &Value) -> u8 {
    match value {
        Value::Null => 0,
        Value::Bool(_) => 1,
        Value::Number(_) if integer_text(value).is_some() => 2,
        Value::Number(_) => 3,
        Value::String(_) => 4,
        Value::Array(_) => 5,
        Value::Object(_) => 6,
    }
}
fn python_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(_), Value::Number(_))
            if integer_text(left).is_some() && integer_text(right).is_some() =>
        {
            compare_integers(left, right) == Some(std::cmp::Ordering::Equal)
        }
        (Value::Number(_), Value::Number(_)) => left.as_f64() == right.as_f64(),
        (Value::Bool(b), Value::Number(_)) => {
            compare_integers(&json!(u8::from(*b)), right) == Some(std::cmp::Ordering::Equal)
        }
        (Value::Number(_), Value::Bool(b)) => {
            compare_integers(left, &json!(u8::from(*b))) == Some(std::cmp::Ordering::Equal)
        }
        (Value::Object(l), Value::Object(r)) => {
            l.len() == r.len()
                && l.iter()
                    .all(|(k, v)| r.get(k).is_some_and(|other| python_equal(v, other)))
        }
        (Value::Array(l), Value::Array(r)) => {
            l.len() == r.len() && l.iter().zip(r).all(|(l, r)| python_equal(l, r))
        }
        _ => left == right,
    }
}
fn difference_paths(left: &Value, right: &Value) -> RevalidationComparison {
    let mut result = RevalidationComparison {
        matched: false,
        difference_paths: Vec::new(),
        truncated: false,
    };
    visit(left, right, "", &mut result);
    result.matched = result.difference_paths.is_empty() && !result.truncated;
    result
}
fn add_path(path: &str, result: &mut RevalidationComparison) {
    if result.difference_paths.len() >= MAX_DIFFERENCE_PATHS {
        result.truncated = true;
    } else {
        result
            .difference_paths
            .push(if path.is_empty() { "/" } else { path }.into());
    }
}
fn visit(left: &Value, right: &Value, path: &str, result: &mut RevalidationComparison) {
    if result.truncated {
        return;
    }
    if python_type(left) != python_type(right) {
        add_path(path, result);
        return;
    }
    match (left, right) {
        (Value::Object(l), Value::Object(r)) => {
            let keys: BTreeSet<_> = l.keys().chain(r.keys()).collect();
            for key in keys {
                let child = format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"));
                if let (Some(l), Some(r)) = (l.get(key), r.get(key)) {
                    visit(l, r, &child, result);
                } else {
                    add_path(&child, result);
                }
            }
        }
        (Value::Array(l), Value::Array(r)) => {
            for index in 0..l.len().max(r.len()) {
                let child = format!("{path}/{index}");
                if let (Some(l), Some(r)) = (l.get(index), r.get(index)) {
                    visit(l, r, &child, result);
                } else {
                    add_path(&child, result);
                }
            }
        }
        _ => {
            if !python_equal(left, right) {
                add_path(path, result);
            }
        }
    }
}
