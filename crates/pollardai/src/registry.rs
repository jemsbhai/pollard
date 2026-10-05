use crate::identity::hash;
use crate::{canonical_bytes, redact, Charges, Error, Result};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

pub type Handler = Arc<dyn Fn(Value) -> Result<Value> + Send + Sync>;
pub type Policy = Arc<dyn Fn(&PolicyContext) -> Decision + Send + Sync>;

/// Frozen action identity with an optional synchronous callback.
#[derive(Clone)]
pub struct ActionSpec {
    name: String,
    version: String,
    description: String,
    schema: Value,
    side_effects: bool,
    handler: Option<Handler>,
    spec_digest: String,
}

impl ActionSpec {
    pub fn new(
        name: impl Into<String>,
        version: impl Into<String>,
        description: impl Into<String>,
        schema: Value,
        side_effects: bool,
        handler: Option<Handler>,
    ) -> Result<Self> {
        let name = name.into();
        let version = version.into();
        let description = description.into();
        if name.is_empty() || version.is_empty() {
            return Err(Error::Invalid(
                "action name and version cannot be empty".into(),
            ));
        }
        check_schema(&schema)?;
        let spec_digest = hash(
            b"",
            &canonical_bytes(
                &json!({"name":name,"version":version,"description":description,"schema":schema,"side_effects":side_effects}),
            )?,
        );
        Ok(Self {
            name,
            version,
            description,
            schema,
            side_effects,
            handler,
            spec_digest,
        })
    }
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn version(&self) -> &str {
        &self.version
    }
    pub fn description(&self) -> &str {
        &self.description
    }
    pub fn schema(&self) -> &Value {
        &self.schema
    }
    pub fn side_effects(&self) -> bool {
        self.side_effects
    }
    pub fn spec_digest(&self) -> &str {
        &self.spec_digest
    }
    pub(crate) fn handler(&self) -> Option<Handler> {
        self.handler.clone()
    }
    pub fn validate_args(&self, args: &Value) -> Result<()> {
        canonical_bytes(args)?;
        if !args.is_object() {
            return Err(Error::Invalid("action arguments must be an object".into()));
        }
        validate_value(args, &self.schema, "$").map_err(Error::Invalid)
    }
    pub fn redact_args(&self, args: &Value) -> Result<Value> {
        canonical_bytes(args)?;
        if !args.is_object() {
            return Err(Error::Invalid("action arguments must be an object".into()));
        }
        redact_sensitive(args, &self.schema)
    }
}

/// Registry identities depend only on sorted spec digests, never callbacks.
#[derive(Clone)]
pub struct Registry {
    specs: BTreeMap<String, ActionSpec>,
    registry_digest: String,
}
impl Registry {
    pub fn new(specs: Vec<ActionSpec>) -> Result<Self> {
        let mut by_name = BTreeMap::new();
        let mut digests = Vec::new();
        for spec in specs {
            digests.push(spec.spec_digest.clone());
            if by_name.insert(spec.name.clone(), spec).is_some() {
                return Err(Error::Invalid("duplicate action name".into()));
            }
        }
        digests.sort();
        let registry_digest = hash(b"", &canonical_bytes(&json!({"spec_digests":digests}))?);
        Ok(Self {
            specs: by_name,
            registry_digest,
        })
    }
    pub fn registry_digest(&self) -> &str {
        &self.registry_digest
    }
    pub fn get(&self, name: &str, version: Option<&str>) -> Result<&ActionSpec> {
        let spec = self
            .specs
            .get(name)
            .ok_or_else(|| Error::NotFound(name.into()))?;
        if version.is_some_and(|v| v != spec.version) {
            return Err(Error::NotFound(format!(
                "{name}@{}",
                version.unwrap_or_default()
            )));
        }
        Ok(spec)
    }
    pub fn iter(&self) -> impl Iterator<Item = &ActionSpec> {
        self.specs.values()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Allow,
    Deny,
    Confirm,
}
/// Policy receives owned snapshots. Its decisions cannot mutate stored identity.
#[derive(Clone)]
pub struct PolicyContext {
    pub spec: ActionSpec,
    pub args: Value,
    pub cursor_id: String,
    pub run_label: String,
    pub counters: Charges,
}

fn unsupported(detail: &str) -> Error {
    Error::UnsupportedSchema(detail.into())
}

fn check_schema(schema: &Value) -> Result<()> {
    canonical_bytes(schema).map_err(|e| unsupported(&e.to_string()))?;
    let obj = schema
        .as_object()
        .ok_or_else(|| unsupported("schema must be an object"))?;
    const KEYS: &[&str] = &[
        "type",
        "properties",
        "required",
        "enum",
        "anyOf",
        "items",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "additionalProperties",
        "title",
        "description",
        "default",
        "sensitive",
    ];
    if let Some(key) = obj.keys().find(|k| !KEYS.contains(&k.as_str())) {
        return Err(unsupported(&format!("unsupported keyword {key}")));
    }
    let ty = obj
        .get("type")
        .map(|v| v.as_str().ok_or_else(|| unsupported("type must be string")))
        .transpose()?;
    if ty.is_some_and(|s| !["object", "string", "integer", "boolean", "array", "null"].contains(&s))
    {
        return Err(unsupported("unsupported type"));
    }
    if let Some(props) = obj.get("properties") {
        for child in props
            .as_object()
            .ok_or_else(|| unsupported("properties must be object"))?
            .values()
        {
            check_schema(child)?;
        }
    }
    if let Some(required) = obj.get("required") {
        let arr = required
            .as_array()
            .ok_or_else(|| unsupported("required must be array"))?;
        if arr.iter().any(|v| !v.is_string()) {
            return Err(unsupported("required must contain strings"));
        }
    }
    if let Some(values) = obj.get("enum") {
        let values = values
            .as_array()
            .filter(|v| !v.is_empty())
            .ok_or_else(|| unsupported("enum must be nonempty array"))?;
        let mut seen = BTreeSet::new();
        for value in values {
            if !seen.insert(canonical_bytes(value)?) {
                return Err(unsupported("enum values must be unique"));
            }
        }
    }
    if let Some(branches) = obj.get("anyOf") {
        for branch in branches
            .as_array()
            .filter(|v| !v.is_empty())
            .ok_or_else(|| unsupported("anyOf must be nonempty array"))?
        {
            check_schema(branch)?;
        }
    }
    if let Some(items) = obj.get("items") {
        check_schema(items)?;
    }
    for (key, required_type, nonnegative) in [
        ("minimum", "integer", false),
        ("maximum", "integer", false),
        ("exclusiveMinimum", "integer", false),
        ("exclusiveMaximum", "integer", false),
        ("minLength", "string", true),
        ("maxLength", "string", true),
        ("minItems", "array", true),
        ("maxItems", "array", true),
    ] {
        if let Some(bound) = obj.get(key) {
            if ty != Some(required_type) {
                return Err(unsupported(&format!("{key} requires type {required_type}")));
            }
            let n = bound
                .as_i64()
                .ok_or_else(|| unsupported(&format!("{key} must be integer")))?;
            if nonnegative && n < 0 {
                return Err(unsupported(&format!("{key} must be nonnegative")));
            }
        }
    }
    for key in ["additionalProperties", "sensitive"] {
        if obj.get(key).is_some_and(|v| !v.is_boolean()) {
            return Err(unsupported(&format!("{key} must be boolean")));
        }
    }
    for key in ["title", "description"] {
        if obj.get(key).is_some_and(|v| !v.is_string()) {
            return Err(unsupported(&format!("{key} must be string")));
        }
    }
    if obj.get("sensitive") == Some(&Value::Bool(true)) && !sensitive_string(schema) {
        return Err(unsupported(
            "only string or nullable string fields may be sensitive",
        ));
    }
    Ok(())
}

fn sensitive_string(schema: &Value) -> bool {
    if schema.get("type").and_then(Value::as_str) == Some("string") {
        return true;
    }
    let Some(branches) = schema.get("anyOf").and_then(Value::as_array) else {
        return false;
    };
    let types: Option<Vec<_>> = branches
        .iter()
        .map(|b| b.get("type").and_then(Value::as_str))
        .collect();
    types.is_some_and(|ts| {
        ts.contains(&"string") && ts.iter().all(|t| ["string", "null"].contains(t))
    })
}

fn matches_type(value: &Value, ty: &str) -> bool {
    match ty {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "boolean" => value.is_boolean(),
        "integer" => value.as_i64().is_some() || value.as_u64().is_some(),
        "null" => value.is_null(),
        _ => false,
    }
}

fn validate_value(value: &Value, schema: &Value, path: &str) -> std::result::Result<(), String> {
    let ty = schema.get("type").and_then(Value::as_str);
    if ty.is_some_and(|t| !matches_type(value, t)) {
        return Err(format!("{path}: expected {}", ty.unwrap_or_default()));
    }
    if schema
        .get("enum")
        .and_then(Value::as_array)
        .is_some_and(|a| !a.contains(value))
    {
        return Err(format!("{path}: value not in enum"));
    }
    if schema
        .get("anyOf")
        .and_then(Value::as_array)
        .is_some_and(|a| !a.iter().any(|s| validate_value(value, s, path).is_ok()))
    {
        return Err(format!("{path}: value does not match anyOf"));
    }
    if ty == Some("integer") {
        let n = value
            .as_i64()
            .ok_or_else(|| format!("{path}: integer exceeds portable range"))?;
        for key in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"] {
            if let Some(b) = schema.get(key).and_then(Value::as_i64) {
                let ok = match key {
                    "minimum" => n >= b,
                    "maximum" => n <= b,
                    "exclusiveMinimum" => n > b,
                    _ => n < b,
                };
                if !ok {
                    return Err(format!("{path}: violates {key}"));
                }
            }
        }
    }
    let len = match ty {
        Some("string") => value
            .as_str()
            .map(|s| (s.chars().count(), "minLength", "maxLength")),
        Some("array") => value.as_array().map(|a| (a.len(), "minItems", "maxItems")),
        _ => None,
    };
    if let Some((len, min, max)) = len {
        if schema
            .get(min)
            .and_then(Value::as_u64)
            .is_some_and(|n| (len as u64) < n)
            || schema
                .get(max)
                .and_then(Value::as_u64)
                .is_some_and(|n| (len as u64) > n)
        {
            return Err(format!("{path}: invalid length"));
        }
    }
    let object = ty == Some("object")
        || (ty.is_none()
            && ["properties", "required", "additionalProperties"]
                .iter()
                .any(|key| schema.get(key).is_some()));
    if object {
        let obj = value
            .as_object()
            .ok_or_else(|| format!("{path}: expected object"))?;
        if let Some(required) = schema.get("required").and_then(Value::as_array) {
            for key in required {
                let key = key.as_str().expect("validated schema");
                if !obj.contains_key(key) {
                    return Err(format!("{path}: missing required property {key}"));
                }
            }
        }
        let props = schema.get("properties").and_then(Value::as_object);
        for (key, value) in obj {
            if let Some(child) = props.and_then(|p| p.get(key)) {
                validate_value(value, child, &format!("{path}.{key}"))?;
            } else if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
                return Err(format!("{path}: unexpected property {key}"));
            }
        }
    }
    if ty == Some("array") {
        if let Some(items) = schema.get("items") {
            for (i, value) in value.as_array().expect("checked array").iter().enumerate() {
                validate_value(value, items, &format!("{path}[{i}]"))?;
            }
        }
    }
    Ok(())
}

fn redact_sensitive(value: &Value, schema: &Value) -> Result<Value> {
    if schema.get("sensitive") == Some(&Value::Bool(true)) && value.is_string() {
        return redact(value, None);
    }
    let mut value = value.clone();
    if let Some(branches) = schema.get("anyOf").and_then(Value::as_array) {
        let matching: Vec<_> = branches
            .iter()
            .filter(|s| validate_value(&value, s, "$").is_ok())
            .collect();
        for branch in if matching.is_empty() {
            branches.iter().collect()
        } else {
            matching
        } {
            value = redact_sensitive(&value, branch)?;
        }
    }
    if let (Some(obj), Some(props)) = (
        value.as_object_mut(),
        schema.get("properties").and_then(Value::as_object),
    ) {
        for (key, schema) in props {
            if let Some(child) = obj.get_mut(key) {
                *child = redact_sensitive(child, schema)?;
            }
        }
    }
    if let (Some(arr), Some(schema)) = (value.as_array_mut(), schema.get("items")) {
        for child in arr {
            *child = redact_sensitive(child, schema)?;
        }
    }
    Ok(value)
}
