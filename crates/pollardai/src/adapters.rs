//! Provider-neutral JSON adapters matching Pollard 1.6.0 normalization.
//!
//! Callers own provider SDKs, credentials, and transport. Pass their JSON
//! responses here before returning them from a runtime callback. Streaming
//! adapters are lazy and reject streams missing a provider terminal event.
use crate::identity::integer_text;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Provider {
    OpenAiResponses,
    OpenAiChat,
    Anthropic,
    Bedrock,
    LiteLlm,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdapterError {
    pub provider: Provider,
    pub event_name: String,
    pub message: String,
    pub raw_event: Box<Value>,
}
impl std::fmt::Display for AdapterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for AdapterError {}
impl From<AdapterError> for crate::Error {
    fn from(error: AdapterError) -> Self {
        // Normalization happens after transport dispatch. Preserve that boundary
        // when a callback uses `?`, so metering cannot refund an unknown outcome.
        Self::OutcomeUnknown(Box::new(Self::Handler(error.to_string())))
    }
}
type AdapterResult<T> = std::result::Result<T, AdapterError>;

fn fail(provider: Provider, event_name: &str, raw_event: Value) -> AdapterError {
    let message = match provider {
        Provider::OpenAiResponses | Provider::OpenAiChat | Provider::LiteLlm => raw_event
            .get("response")
            .and_then(|r| r.get("error"))
            .filter(|e| e.is_object())
            .or_else(|| raw_event.get("error"))
            .and_then(|e| e.get("message"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("OpenAI generation stream ended without a terminal event")
            .to_owned(),
        Provider::Anthropic => raw_event
            .get("error")
            .and_then(|e| e.get("message"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Anthropic Messages stream ended without message_stop")
            .to_owned(),
        Provider::Bedrock => {
            let detail = raw_event.get(event_name).unwrap_or(&Value::Null);
            let detail = if detail.is_object() {
                detail.get("message").unwrap_or(&Value::Null)
            } else {
                detail
            };
            let text = if detail.is_null() {
                "None".to_owned()
            } else {
                detail
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| detail.to_string())
            };
            format!("Bedrock stream {event_name}: {text}")
        }
    };
    AdapterError {
        provider,
        event_name: event_name.into(),
        message,
        raw_event: Box::new(raw_event),
    }
}

fn object(value: Value, provider: Provider) -> AdapterResult<Map<String, Value>> {
    match value {
        Value::Object(obj) => Ok(obj),
        raw => Err(AdapterError {
            provider,
            event_name: "invalid_response".into(),
            message: "provider response must be an object".into(),
            raw_event: Box::new(raw),
        }),
    }
}
fn valid_int(value: &Value) -> bool {
    integer_text(value).is_some_and(|s| !s.starts_with('-'))
}
fn int_field(value: &Value, names: &[&str]) -> Value {
    names
        .iter()
        .filter_map(|name| value.get(*name))
        .find(|v| valid_int(v))
        .cloned()
        .unwrap_or_else(|| json!(0))
}
fn sum_fields(values: &[Value]) -> Value {
    // Python accepts unbounded token integers. Sum decimal digits without a
    // float conversion or overflowing Rust's machine-sized counters.
    let mut total = vec![0u8];
    for value in values {
        let digits = integer_text(value).expect("nonnegative int");
        let mut carry = 0u8;
        for (i, digit) in digits.bytes().rev().enumerate() {
            if total.len() <= i {
                total.push(0);
            }
            let n = total[i] + digit - b'0' + carry;
            total[i] = n % 10;
            carry = n / 10;
        }
        let mut i = digits.len();
        while carry > 0 {
            if total.len() <= i {
                total.push(0);
            }
            let n = total[i] + carry;
            total[i] = n % 10;
            carry = n / 10;
            i += 1;
        }
    }
    let text: String = total.iter().rev().map(|n| (n + b'0') as char).collect();
    serde_json::from_str(&text).expect("integer sum")
}

pub fn openai_usage(response: &Value) -> Value {
    let usage = &response["usage"];
    json!({"input_tokens":int_field(usage, &["input_tokens","prompt_tokens"]),"output_tokens":int_field(usage, &["output_tokens","completion_tokens"])})
}
pub fn anthropic_usage(response: &Value) -> Value {
    let usage = &response["usage"];
    json!({"input_tokens":sum_fields(&[int_field(usage, &["input_tokens"]),int_field(usage, &["cache_creation_input_tokens"]),int_field(usage, &["cache_read_input_tokens"])]),"output_tokens":int_field(usage, &["output_tokens"])})
}
pub fn bedrock_usage(usage: &Value) -> Value {
    json!({"input_tokens":sum_fields(&[int_field(usage, &["inputTokens","input_tokens"]),int_field(usage, &["cacheReadInputTokens","cache_read_input_tokens"]),int_field(usage, &["cacheWriteInputTokens","cache_write_input_tokens"])]),"output_tokens":int_field(usage, &["outputTokens","output_tokens"])})
}

fn usage_fields(
    provider: Provider,
) -> (&'static [&'static [&'static str]], &'static [&'static str]) {
    match provider {
        Provider::Anthropic => (
            &[&["input_tokens"], &["output_tokens"]],
            &["cache_creation_input_tokens", "cache_read_input_tokens"],
        ),
        Provider::Bedrock => (
            &[
                &["inputTokens", "input_tokens"],
                &["outputTokens", "output_tokens"],
            ],
            &[
                "cacheReadInputTokens",
                "cache_read_input_tokens",
                "cacheWriteInputTokens",
                "cache_write_input_tokens",
            ],
        ),
        _ => (
            &[
                &["input_tokens", "prompt_tokens"],
                &["output_tokens", "completion_tokens"],
            ],
            &[],
        ),
    }
}
fn normalize_usage(result: &mut Map<String, Value>, provider: Provider) {
    let Some(usage) = result.remove("usage").filter(Value::is_object) else {
        return;
    };
    let (required, optional) = usage_fields(provider);
    let valid = required.iter().all(|alternatives| {
        alternatives
            .iter()
            .any(|name| usage.get(*name).is_some_and(valid_int))
    }) && optional
        .iter()
        .all(|name| usage.get(*name).map_or(true, valid_int));
    if valid {
        let normalized = match provider {
            Provider::Anthropic => anthropic_usage(&json!({"usage":usage})),
            Provider::Bedrock => bedrock_usage(&usage),
            _ => openai_usage(&json!({"usage":usage})),
        };
        result.insert("usage".into(), normalized);
    }
    result.insert("provider_usage".into(), usage);
}
fn select(value: &Value, keys: &[&str]) -> Value {
    Value::Object(
        keys.iter()
            .filter_map(|key| value.get(*key).map(|v| ((*key).into(), v.clone())))
            .collect(),
    )
}

/// Merge caller-owned request defaults, then remove Pollard's private metadata.
pub fn merge_request(defaults: &Value, payload: &Value) -> crate::Result<Value> {
    let mut params = defaults
        .as_object()
        .cloned()
        .ok_or_else(|| crate::Error::Invalid("request defaults must be an object".into()))?;
    params.extend(
        payload
            .as_object()
            .ok_or_else(|| crate::Error::Invalid("request payload must be an object".into()))?
            .clone(),
    );
    params.remove("_pollard");
    Ok(Value::Object(params))
}

/// Normalize JSON returned by a caller-owned provider client.
pub fn normalize_response(provider: Provider, response: Value) -> AdapterResult<Value> {
    let mut result = object(response, provider)?;
    if provider == Provider::OpenAiResponses && result.get("status") == Some(&json!("failed")) {
        return Err(fail(
            provider,
            "response.failed",
            json!({"type":"response.failed", "response":result}),
        ));
    }
    normalize_usage(&mut result, provider);
    let mut text = String::new();
    let mut texts_seen = false;
    let mut tools = Vec::new();
    match provider {
        Provider::OpenAiChat | Provider::LiteLlm => {
            if let Some(message) = result
                .get("choices")
                .and_then(Value::as_array)
                .and_then(|a| a.first())
                .and_then(|v| v.get("message"))
            {
                if let Some(content) = message.get("content").and_then(Value::as_str) {
                    text.push_str(content);
                    texts_seen = true;
                }
                if let Some(calls) = message.get("tool_calls").filter(|v| v.is_array()).cloned() {
                    result.insert("tool_calls".into(), calls);
                }
            }
        }
        Provider::OpenAiResponses => {
            if let Some(output_text) = result.get("output_text").and_then(Value::as_str) {
                text.push_str(output_text);
            } else if let Some(output) = result.get("output").and_then(Value::as_array) {
                for item in output {
                    if item.get("type") == Some(&json!("message")) {
                        if let Some(content) = item.get("content").and_then(Value::as_array) {
                            for block in content {
                                if block.get("type") == Some(&json!("output_text")) {
                                    if let Some(part) = block.get("text").and_then(Value::as_str) {
                                        text.push_str(part);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if let Some(output) = result.get("output").and_then(Value::as_array) {
                for item in output {
                    if item.get("type") == Some(&json!("function_call")) {
                        tools.push(select(item, &["call_id", "name", "arguments"]));
                    }
                }
            }
        }
        Provider::Anthropic => {
            if let Some(content) = result.get("content").and_then(Value::as_array) {
                for block in content {
                    if block.get("type") == Some(&json!("text")) {
                        if let Some(part) = block.get("text").and_then(Value::as_str) {
                            text.push_str(part);
                            texts_seen = true;
                        }
                    } else if block.get("type") == Some(&json!("tool_use")) {
                        tools.push(select(block, &["id", "name", "input"]));
                    }
                }
            }
        }
        Provider::Bedrock => {
            if let Some(content) = result
                .get("output")
                .and_then(|v| v.get("message"))
                .and_then(|v| v.get("content"))
                .and_then(Value::as_array)
            {
                for block in content {
                    if let Some(part) = block.get("text").and_then(Value::as_str) {
                        text.push_str(part);
                    }
                    if let Some(tool) = block.get("toolUse").filter(|v| v.is_object()) {
                        tools.push(select(tool, &["toolUseId", "name", "input"]));
                    }
                }
            }
        }
    }
    if !text.is_empty() || texts_seen {
        result.insert("text".into(), json!(text));
    }
    if !tools.is_empty() {
        result.insert("tool_calls".into(), json!(tools));
    }
    Ok(Value::Object(result))
}

/// Lazily normalize a synchronous iterator of JSON stream events.
pub fn normalize_stream<I: IntoIterator<Item = Value>>(
    provider: Provider,
    events: I,
) -> AdapterStream<I::IntoIter> {
    AdapterStream {
        events: events.into_iter(),
        normalizer: StreamNormalizer::new(provider),
    }
}

pub struct AdapterStream<I> {
    events: I,
    normalizer: StreamNormalizer,
}

/// Incremental provider normalization for caller-owned async or sync streams.
/// Call `push` for each event, then `finish` exactly when transport ends. No
/// executor, provider SDK, or buffering of the full event sequence is required.
pub struct StreamNormalizer {
    provider: Provider,
    state: StreamState,
    finished: bool,
}
#[derive(Default)]
struct StreamState {
    text: String,
    tools: BTreeMap<StreamIndex, Value>,
    fields: Map<String, Value>,
    provider_usage: Map<String, Value>,
    input: Value,
    output: Value,
    input_valid: bool,
    output_valid: bool,
    completed: bool,
}

impl<I: Iterator<Item = Value>> Iterator for AdapterStream<I> {
    type Item = AdapterResult<Value>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.normalizer.finished {
            return None;
        }
        if let Some(raw) = self.events.next() {
            return Some(self.normalizer.push(raw));
        }
        self.normalizer.finish().transpose()
    }
}

impl StreamNormalizer {
    pub fn new(provider: Provider) -> Self {
        Self {
            provider,
            state: StreamState::default(),
            finished: false,
        }
    }
    pub fn push(&mut self, raw: Value) -> AdapterResult<Value> {
        if self.finished {
            return Err(AdapterError {
                provider: self.provider,
                event_name: "stream_closed".into(),
                message: "provider stream is already closed".into(),
                raw_event: Box::new(raw),
            });
        }
        if !raw.is_object() {
            self.finished = true;
            return object(raw, self.provider).map(Value::Object);
        }
        let result = stream_event(self.provider, &mut self.state, raw);
        if result.is_err() {
            self.finished = true;
        }
        result
    }
    pub fn finish(&mut self) -> AdapterResult<Option<Value>> {
        if self.finished {
            return Ok(None);
        }
        self.finished = true;
        if !self.state.completed {
            let (name, event) = match self.provider {
                Provider::OpenAiResponses => (
                    "response.stream_ended",
                    json!({"type":"response.stream_ended"}),
                ),
                Provider::OpenAiChat | Provider::LiteLlm => (
                    "chat.completion.stream_ended",
                    json!({"type":"chat.completion.stream_ended"}),
                ),
                Provider::Anthropic => ("stream_ended", json!({"type":"stream_ended"})),
                Provider::Bedrock => ("streamEnded", json!({"streamEnded":{}})),
            };
            return Err(fail(self.provider, name, event));
        }
        if self.provider == Provider::OpenAiResponses {
            Ok(None)
        } else {
            Ok(Some(
                json!({"result":stream_final(self.provider, &mut self.state)}),
            ))
        }
    }
}

fn stream_event(provider: Provider, state: &mut StreamState, raw: Value) -> AdapterResult<Value> {
    let mut chunk = json!({"event":raw});
    match provider {
        Provider::OpenAiResponses => {
            let event = raw["type"].as_str().unwrap_or("");
            if event == "response.failed" {
                return Err(fail(provider, "response.failed", raw));
            }
            if event == "response.output_text.delta" {
                if let Some(text) = raw["delta"].as_str() {
                    state.text.push_str(text);
                    chunk["delta"] = json!({"text":text});
                }
            }
            if ["response.completed", "response.incomplete"].contains(&event) {
                chunk["result"] = if raw["response"].is_null() {
                    json!({"text":state.text})
                } else {
                    normalize_response(provider, raw["response"].clone())?
                };
                state.completed = true;
            }
        }
        Provider::OpenAiChat | Provider::LiteLlm => {
            for key in ["id", "model"] {
                copy_string(&raw, key, &mut state.fields, key);
            }
            stream_usage(provider, state, raw.get("usage"));
            if let Some(choice) = raw["choices"].as_array().and_then(|a| a.first()) {
                if let Some(reason) = choice["finish_reason"].as_str() {
                    state.fields.insert("finish_reason".into(), json!(reason));
                    state.completed = true;
                }
                let delta = &choice["delta"];
                if let Some(text) = delta["content"].as_str() {
                    state.text.push_str(text);
                    chunk["delta"] = json!({"text":text});
                }
                if let Some(calls) = delta["tool_calls"].as_array() {
                    for fragment in calls.iter().filter(|v| v.is_object()) {
                        let index = python_index(fragment.get("index"))
                            .unwrap_or_else(|| StreamIndex("0".into()));
                        let call = state
                            .tools
                            .entry(index)
                            .or_insert_with(|| json!({"function":{"name":"","arguments":""}}));
                        for key in ["id", "type"] {
                            if let Some(text) = fragment[key].as_str() {
                                call[key] = json!(text);
                            }
                        }
                        for key in ["name", "arguments"] {
                            append_string(
                                &mut call["function"][key],
                                fragment["function"].get(key),
                            );
                        }
                    }
                }
            }
        }
        Provider::Anthropic => {
            let event = raw["type"].as_str().unwrap_or("");
            match event {
                "error" => return Err(fail(provider, "error", raw)),
                "message_start" => {
                    let message = &raw["message"];
                    for key in ["id", "model"] {
                        copy_string(message, key, &mut state.fields, key);
                    }
                    if let Some(usage) = message["usage"].as_object() {
                        state.provider_usage.extend(usage.clone());
                        state.input_valid = usage.get("input_tokens").is_some_and(valid_int)
                            && ["cache_creation_input_tokens", "cache_read_input_tokens"]
                                .iter()
                                .all(|key| usage.get(*key).map_or(true, valid_int));
                        state.input = anthropic_usage(message)["input_tokens"].clone();
                    }
                }
                "content_block_start" => {
                    if let Some(index) = python_index(raw.get("index")) {
                        let block = &raw["content_block"];
                        if block["type"] == "text" {
                            if let Some(text) = block["text"].as_str() {
                                state.text.push_str(text);
                            }
                        } else if block["type"] == "tool_use" {
                            state.tools.insert(
                                index,
                                json!({"id":block["id"],"name":block["name"],"input_json":""}),
                            );
                        }
                    }
                }
                "content_block_delta" => {
                    if let Some(index) = python_index(raw.get("index")) {
                        let delta = &raw["delta"];
                        if delta["type"] == "text_delta" {
                            if let Some(text) = delta["text"].as_str() {
                                state.text.push_str(text);
                                chunk["delta"] = json!({"text":text});
                            }
                        } else if delta["type"] == "input_json_delta" {
                            if let Some(tool) = state.tools.get_mut(&index) {
                                append_string(&mut tool["input_json"], delta.get("partial_json"));
                            }
                        }
                    }
                }
                "message_delta" => {
                    copy_string(
                        &raw["delta"],
                        "stop_reason",
                        &mut state.fields,
                        "stop_reason",
                    );
                    if let Some(usage) = raw["usage"].as_object() {
                        state.provider_usage.extend(usage.clone());
                        state.output_valid = usage.get("output_tokens").is_some_and(valid_int);
                        state.output = int_field(&raw["usage"], &["output_tokens"]);
                    }
                }
                "message_stop" => state.completed = true,
                _ => {}
            }
        }
        Provider::Bedrock => {
            if let Some(name) = raw
                .as_object()
                .expect("object")
                .keys()
                .find(|name| name.ends_with("Exception"))
            {
                return Err(fail(provider, &name.clone(), raw));
            }
            copy_string(&raw["messageStart"], "role", &mut state.fields, "role");
            let start = &raw["contentBlockStart"];
            if let Some(index) = python_index(start.get("contentBlockIndex")) {
                let tool = &start["start"]["toolUse"];
                if tool.is_object() {
                    state.tools.insert(index, json!({"toolUseId":tool.get("toolUseId").unwrap_or(&json!("")),"name":tool.get("name").unwrap_or(&json!("")),"input":""}));
                }
            }
            let block = &raw["contentBlockDelta"];
            if let Some(index) = python_index(block.get("contentBlockIndex")) {
                let delta = &block["delta"];
                if let Some(text) = delta["text"].as_str() {
                    state.text.push_str(text);
                    chunk["delta"] = json!({"text":text});
                }
                if let Some(fragment) = delta["toolUse"]["input"].as_str() {
                    let tool = state
                        .tools
                        .entry(index)
                        .or_insert_with(|| json!({"toolUseId":"","name":"","input":""}));
                    append_string(&mut tool["input"], Some(&json!(fragment)));
                    chunk["delta"] = json!({"tool_call":{"index":block.get("contentBlockIndex").cloned().unwrap_or_else(|| json!(0)),"input":fragment}});
                }
            }
            if let Some(reason) = raw["messageStop"]["stopReason"].as_str() {
                state.fields.insert("stopReason".into(), json!(reason));
                state.completed = true;
            }
            stream_usage(provider, state, raw["metadata"].get("usage"));
            if let Some(metrics) = raw["metadata"]["metrics"].as_object() {
                if metrics.is_empty() {
                    state.fields.remove("metrics");
                } else {
                    state.fields.insert("metrics".into(), json!(metrics));
                }
            }
        }
    }
    Ok(chunk)
}
#[derive(Debug, Clone, PartialEq, Eq)]
struct StreamIndex(String);
impl PartialOrd for StreamIndex {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for StreamIndex {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        let (left, right) = (&self.0, &other.0);
        let (ln, rn) = (left.starts_with('-'), right.starts_with('-'));
        if ln != rn {
            return if ln {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Greater
            };
        }
        let (left, right) = (left.trim_start_matches('-'), right.trim_start_matches('-'));
        let order = left.len().cmp(&right.len()).then_with(|| left.cmp(right));
        if ln {
            order.reverse()
        } else {
            order
        }
    }
}
fn python_index(value: Option<&Value>) -> Option<StreamIndex> {
    match value {
        None => Some(StreamIndex("0".into())),
        Some(Value::Bool(b)) => Some(StreamIndex(if *b { "1" } else { "0" }.into())),
        Some(v) => integer_text(v).map(StreamIndex),
    }
}
fn copy_string(source: &Value, key: &str, target: &mut Map<String, Value>, target_key: &str) {
    if let Some(value) = source.get(key).and_then(Value::as_str) {
        target.insert(target_key.into(), json!(value));
    }
}
fn append_string(target: &mut Value, fragment: Option<&Value>) {
    if let Some(fragment) = fragment.and_then(Value::as_str) {
        *target = json!(format!("{}{fragment}", target.as_str().unwrap_or("")));
    }
}
fn stream_usage(provider: Provider, state: &mut StreamState, usage: Option<&Value>) {
    if let Some(usage) = usage.filter(|v| v.is_object()) {
        let mut value = Map::new();
        value.insert("usage".into(), usage.clone());
        normalize_usage(&mut value, provider);
        for key in ["provider_usage", "usage"] {
            if let Some(value) = value.remove(key) {
                state.fields.insert(key.into(), value);
            } else {
                state.fields.remove(key);
            }
        }
    }
}
fn stream_final(provider: Provider, state: &mut StreamState) -> Value {
    let mut result = std::mem::take(&mut state.fields);
    result.insert("text".into(), json!(state.text));
    if provider == Provider::Anthropic {
        if state.input_valid && state.output_valid {
            result.insert(
                "usage".into(),
                json!({"input_tokens":state.input,"output_tokens":state.output}),
            );
        }
        if !state.provider_usage.is_empty() {
            result.insert("provider_usage".into(), json!(state.provider_usage));
        }
    }
    if !state.tools.is_empty() {
        let mut calls = Vec::new();
        for tool in state.tools.values() {
            let mut tool = tool.clone();
            match provider {
                Provider::Anthropic => {
                    for key in ["id", "name"] {
                        if !tool[key].is_string() {
                            tool.as_object_mut().expect("tool").remove(key);
                        }
                    }
                    let source = tool["input_json"].as_str().unwrap_or("");
                    if let Ok(parsed) =
                        serde_json::from_str::<Value>(if source.is_empty() { "{}" } else { source })
                    {
                        tool.as_object_mut().expect("tool").remove("input_json");
                        tool["input"] = parsed;
                    }
                }
                Provider::Bedrock => {
                    if let Some(source) = tool["input"].as_str() {
                        if let Ok(parsed) = serde_json::from_str::<Value>(source) {
                            tool["input"] = parsed;
                        }
                    }
                }
                _ => {}
            }
            calls.push(tool);
        }
        result.insert("tool_calls".into(), json!(calls));
    }
    Value::Object(result)
}
