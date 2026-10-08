use crate::identity::{compare_integers, hash, integer_text};
use crate::{canonical_bytes, redact, Error, MeterCharges, Result};
use serde_json::{json, Value};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

pub type Handler = Arc<dyn Fn(Value) -> Result<Value> + Send + Sync>;
pub type HandlerFuture = Pin<Box<dyn Future<Output = Result<Value>> + Send>>;
pub type AsyncHandler = Arc<dyn Fn(Value) -> HandlerFuture + Send + Sync>;
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
    async_handler: Option<AsyncHandler>,
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
        let schema = if schema_has_local_refs(&schema) {
            resolve_local_refs(&schema)?
        } else {
            schema
        };
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
            async_handler: None,
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
    /// Attach an async callback without changing the frozen action identity.
    /// A synchronous call still requires its own synchronous handler.
    pub fn with_async_handler(mut self, handler: AsyncHandler) -> Self {
        self.async_handler = Some(handler);
        self
    }
    pub(crate) fn async_handler(&self) -> Option<AsyncHandler> {
        self.async_handler.clone()
    }
    pub fn validate_args(&self, args: &Value) -> Result<()> {
        canonical_bytes(args)?;
        validate_value(args, &self.schema, "$").map_err(Error::Invalid)
    }
    pub fn redact_args(&self, args: &Value) -> Result<Value> {
        let redacted = redact_sensitive(args, &self.schema)?;
        if !redacted.is_object() {
            return Err(Error::Invalid(
                "action arguments must redact to an object".into(),
            ));
        }
        Ok(redacted)
    }
}

/// Registry identities depend only on sorted spec digests, never callbacks.
#[derive(Clone)]
pub struct Registry {
    specs: BTreeMap<String, ActionSpec>,
    order: Vec<String>,
    registry_digest: String,
}
impl Registry {
    pub fn new(specs: Vec<ActionSpec>) -> Result<Self> {
        let mut by_name = BTreeMap::new();
        let mut digests = Vec::new();
        let mut order = Vec::new();
        for spec in specs {
            digests.push(spec.spec_digest.clone());
            order.push(spec.name.clone());
            if by_name.insert(spec.name.clone(), spec).is_some() {
                return Err(Error::Invalid("duplicate action name".into()));
            }
        }
        digests.sort();
        let registry_digest = hash(b"", &canonical_bytes(&json!({"spec_digests":digests}))?);
        Ok(Self {
            specs: by_name,
            order,
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
        self.order.iter().map(|name| &self.specs[name])
    }
    pub fn contains(&self, name: &str) -> bool {
        self.specs.contains_key(name)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
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
    pub counters: MeterCharges,
}

fn unsupported(detail: &str) -> Error {
    Error::UnsupportedSchema(detail.into())
}

/// Whether reference/definition keywords occur in schema positions.
/// Reference-shaped values in enum/default annotations remain literal data.
pub fn schema_has_local_refs(schema: &Value) -> bool {
    let Some(obj) = schema.as_object() else {
        return false;
    };
    ["$ref", "$defs", "definitions"]
        .iter()
        .any(|key| obj.contains_key(*key))
        || obj
            .get("properties")
            .and_then(Value::as_object)
            .is_some_and(|props| props.values().any(schema_has_local_refs))
        || obj
            .get("anyOf")
            .and_then(Value::as_array)
            .is_some_and(|branches| branches.iter().any(schema_has_local_refs))
        || obj.get("items").is_some_and(schema_has_local_refs)
}

/// Expand finite local JSON Pointer references as Pollard 1.6.0 does.
/// Remote, dangling, malformed, and recursive references fail at registration.
pub fn resolve_local_refs(schema: &Value) -> Result<Value> {
    let resolved = resolve_schema(schema, schema, "$", &mut Vec::new())?;
    if !resolved.is_object() {
        return Err(unsupported("schema must resolve to an object"));
    }
    Ok(resolved)
}

fn resolve_schema(
    value: &Value,
    root: &Value,
    path: &str,
    active: &mut Vec<String>,
) -> Result<Value> {
    let Some(obj) = value.as_object() else {
        return Ok(value.clone());
    };
    if let Some(reference) = obj.get("$ref").filter(|v| !v.is_null()) {
        let reference = reference
            .as_str()
            .ok_or_else(|| unsupported(&format!("{path}.$ref: must be a string")))?;
        if obj.keys().any(|key| {
            ![
                "$defs",
                "$ref",
                "default",
                "definitions",
                "description",
                "sensitive",
                "title",
            ]
            .contains(&key.as_str())
        }) {
            return Err(unsupported(&format!(
                "{path}.$ref: unsupported sibling keywords"
            )));
        }
        let pointer = local_pointer(reference, path)?;
        if active.contains(&pointer) {
            return Err(unsupported(&format!("{path}.$ref: cyclic local reference")));
        }
        let target = pointer_target(root, &pointer, path)?;
        active.push(pointer);
        let expanded = resolve_schema(target, root, &format!("reference {reference}"), active);
        active.pop();
        let mut combined = expanded?
            .as_object()
            .cloned()
            .ok_or_else(|| unsupported(&format!("{path}.$ref: target must be a schema object")))?;
        for (name, sibling) in obj {
            if !["$defs", "$ref", "definitions"].contains(&name.as_str()) {
                combined.insert(
                    name.clone(),
                    resolve_schema(sibling, root, &format!("{path}.{name}"), active)?,
                );
            }
        }
        return Ok(Value::Object(combined));
    }
    let mut children = serde_json::Map::new();
    for (name, child) in obj {
        if ["$defs", "definitions"].contains(&name.as_str()) {
            continue;
        }
        let resolved = match name.as_str() {
            "properties" if child.is_object() => {
                let mut props = serde_json::Map::new();
                for (key, schema) in child.as_object().expect("object") {
                    props.insert(
                        key.clone(),
                        resolve_schema(schema, root, &format!("{path}.properties.{key}"), active)?,
                    );
                }
                Value::Object(props)
            }
            "anyOf" if child.is_array() => Value::Array(
                child
                    .as_array()
                    .expect("array")
                    .iter()
                    .enumerate()
                    .map(|(i, schema)| {
                        resolve_schema(schema, root, &format!("{path}.anyOf[{i}]"), active)
                    })
                    .collect::<Result<Vec<_>>>()?,
            ),
            "items" => resolve_schema(child, root, &format!("{path}.items"), active)?,
            _ => child.clone(),
        };
        children.insert(name.clone(), resolved);
    }
    Ok(Value::Object(children))
}

fn local_pointer(reference: &str, path: &str) -> Result<String> {
    if reference == "#" {
        return Ok(String::new());
    }
    if !reference.starts_with("#/") {
        return Err(unsupported(&format!(
            "{path}.$ref: only local JSON Pointer references are supported"
        )));
    }
    let invalid = || unsupported(&format!("{path}.$ref: invalid percent escape"));
    let mut decoded = Vec::new();
    let fragment = &reference.as_bytes()[1..];
    let mut i = 0;
    while i < fragment.len() {
        if fragment[i] == b'%' {
            let hi = fragment
                .get(i + 1)
                .and_then(|c| (*c as char).to_digit(16))
                .ok_or_else(invalid)?;
            let lo = fragment
                .get(i + 2)
                .and_then(|c| (*c as char).to_digit(16))
                .ok_or_else(invalid)?;
            decoded.push((hi * 16 + lo) as u8);
            i += 3;
        } else {
            decoded.push(fragment[i]);
            i += 1;
        }
    }
    String::from_utf8(decoded).map_err(|_| invalid())
}

fn pointer_target<'a>(root: &'a Value, pointer: &str, path: &str) -> Result<&'a Value> {
    let mut current = root;
    if pointer.is_empty() {
        return Ok(current);
    }
    let missing = || unsupported(&format!("{path}.$ref: missing local reference target"));
    for raw in pointer.strip_prefix('/').unwrap_or(pointer).split('/') {
        let mut token = String::new();
        let mut chars = raw.chars();
        while let Some(c) = chars.next() {
            if c == '~' {
                token.push(match chars.next() {
                    Some('0') => '~',
                    Some('1') => '/',
                    _ => {
                        return Err(unsupported(&format!(
                            "{path}.$ref: invalid JSON Pointer escape"
                        )))
                    }
                });
            } else {
                token.push(c);
            }
        }
        current = match current {
            Value::Object(obj) => obj.get(&token).ok_or_else(missing)?,
            Value::Array(arr) if !token.is_empty() && token.bytes().all(|c| c.is_ascii_digit()) => {
                arr.get(token.parse::<usize>().map_err(|_| missing())?)
                    .ok_or_else(missing)?
            }
            _ => return Err(missing()),
        };
    }
    Ok(current)
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
        .filter(|v| !v.is_null())
        .map(|v| v.as_str().ok_or_else(|| unsupported("type must be string")))
        .transpose()?;
    if ty.is_some_and(|s| !["object", "string", "integer", "boolean", "array", "null"].contains(&s))
    {
        return Err(unsupported("unsupported type"));
    }
    if let Some(props) = obj.get("properties").filter(|v| !v.is_null()) {
        for child in props
            .as_object()
            .ok_or_else(|| unsupported("properties must be object"))?
            .values()
        {
            check_schema(child)?;
        }
    }
    if let Some(required) = obj.get("required").filter(|v| !v.is_null()) {
        let arr = required
            .as_array()
            .ok_or_else(|| unsupported("required must be array"))?;
        if arr.iter().any(|v| !v.is_string()) {
            return Err(unsupported("required must contain strings"));
        }
    }
    if let Some(values) = obj.get("enum").filter(|v| !v.is_null()) {
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
    if let Some(branches) = obj.get("anyOf").filter(|v| !v.is_null()) {
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
            let n = integer_text(bound)
                .ok_or_else(|| unsupported(&format!("{key} must be integer")))?;
            if nonnegative && n.starts_with('-') {
                return Err(unsupported(&format!("{key} must be nonnegative")));
            }
        }
    }
    for key in ["additionalProperties", "sensitive"] {
        if obj
            .get(key)
            .is_some_and(|v| !v.is_null() && !v.is_boolean())
        {
            return Err(unsupported(&format!("{key} must be boolean")));
        }
    }
    for key in ["title", "description"] {
        if obj.get(key).is_some_and(|v| !v.is_null() && !v.is_string()) {
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
        "integer" => integer_text(value).is_some(),
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
        .is_some_and(|a| {
            !a.iter()
                .any(|v| canonical_bytes(v).ok() == canonical_bytes(value).ok())
        })
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
        for key in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"] {
            if let Some(b) = schema.get(key) {
                let order = compare_integers(value, b).expect("validated integer schema");
                let ok = match key {
                    "minimum" => order != Ordering::Less,
                    "maximum" => order != Ordering::Greater,
                    "exclusiveMinimum" => order == Ordering::Greater,
                    _ => order == Ordering::Less,
                };
                if !ok {
                    let condition = match key {
                        "minimum" => "at least",
                        "maximum" => "at most",
                        "exclusiveMinimum" => "greater than",
                        _ => "less than",
                    };
                    return Err(format!("{path}: value must be {condition} {b}"));
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
        for (key, violation, condition) in [
            (min, Ordering::Less, "at least"),
            (max, Ordering::Greater, "at most"),
        ] {
            if let Some(bound) = schema.get(key) {
                if compare_integers(&json!(len), bound) == Some(violation) {
                    let measure = if ty == Some("array") {
                        "item count"
                    } else {
                        "length"
                    };
                    return Err(format!("{path}: {measure} must be {condition} {bound}"));
                }
            }
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
        let empty = serde_json::Map::new();
        let properties = schema.get("properties");
        let props = if properties.is_none() {
            Some(&empty)
        } else {
            properties.and_then(Value::as_object)
        };
        if let Some(props) = props {
            for (key, child) in props {
                if let Some(value) = obj.get(key) {
                    validate_value(value, child, &format!("{path}.{key}"))?;
                }
            }
            if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
                if let Some(key) = obj.keys().filter(|key| !props.contains_key(*key)).min() {
                    return Err(format!("{path}: unexpected property {key}"));
                }
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
