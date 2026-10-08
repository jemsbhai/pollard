//! Approximate input-token counting using embedded tiktoken dictionaries.
//! Matches Python's textual-leaf and message-overhead algorithm. Images, tools,
//! provider-added instructions and wire-format changes may add tokens.
use crate::{Error, Result, TokenEstimator, Value};
use std::{cell::RefCell, collections::BTreeMap, rc::Rc};
use tiktoken_rs::{
    tokenizer::{get_tokenizer, Tokenizer},
    CoreBPE,
};

pub struct OpenAiTokenEstimator {
    model: Option<String>,
    tokens_per_message: u64,
    encodings: RefCell<BTreeMap<String, CoreBPE>>,
}
impl OpenAiTokenEstimator {
    pub fn new(model: Option<String>, tokens_per_message: u64) -> Self {
        Self {
            model,
            tokens_per_message,
            encodings: RefCell::new(BTreeMap::new()),
        }
    }
    pub fn estimate_input_tokens(&self, payload: &Value) -> Result<u64> {
        let model = self
            .model
            .as_deref()
            .filter(|model| !model.is_empty())
            .or_else(|| payload.get("model").and_then(Value::as_str));
        let fallback = fallback_encoding_name(model);
        let tokenizer = model
            .and_then(get_tokenizer)
            .unwrap_or(if fallback == "o200k_base" {
                Tokenizer::O200kBase
            } else {
                Tokenizer::Cl100kBase
            });
        let key = format!("{tokenizer:?}");
        let mut encodings = self.encodings.borrow_mut();
        if !encodings.contains_key(&key) {
            encodings.insert(
                key.clone(),
                tiktoken_rs::get_bpe_from_tokenizer(tokenizer)
                    .map_err(|e| Error::Invalid(format!("cannot initialize tokenizer: {e}")))?,
            );
        }
        let encoding = &encodings[&key];
        let mut count = 0u64;
        let mut pending = vec![(payload, None)];
        while let Some((value, key)) = pending.pop() {
            match value {
                Value::String(text) if key != Some("model") => {
                    // Python encoding.encode rejects recognized special tokens.
                    let special: &[&str] = match tokenizer {
                        Tokenizer::O200kBase => &[tiktoken_rs::ENDOFTEXT, tiktoken_rs::ENDOFPROMPT],
                        Tokenizer::Cl100kBase => &[
                            tiktoken_rs::ENDOFTEXT,
                            tiktoken_rs::ENDOFPROMPT,
                            tiktoken_rs::FIM_PREFIX,
                            tiktoken_rs::FIM_MIDDLE,
                            tiktoken_rs::FIM_SUFFIX,
                        ],
                        Tokenizer::P50kEdit => &[
                            tiktoken_rs::ENDOFTEXT,
                            tiktoken_rs::FIM_PREFIX,
                            tiktoken_rs::FIM_MIDDLE,
                            tiktoken_rs::FIM_SUFFIX,
                        ],
                        _ => &[tiktoken_rs::ENDOFTEXT],
                    };
                    if special.iter().any(|token| text.contains(*token)) {
                        return Err(Error::Invalid(
                            "input contains a disallowed special token".into(),
                        ));
                    }
                    count = count
                        .checked_add(encoding.encode_ordinary(text).len() as u64)
                        .ok_or_else(|| Error::Invalid("token count overflow".into()))?;
                }
                Value::Array(values) => pending.extend(values.iter().map(|v| (v, None))),
                Value::Object(values) => {
                    pending.extend(values.iter().map(|(k, v)| (v, Some(k.as_str()))))
                }
                _ => {}
            }
        }
        let messages = payload
            .get("messages")
            .and_then(Value::as_array)
            .map_or(0, |v| v.len() as u64);
        count
            .checked_add(
                messages
                    .checked_mul(self.tokens_per_message)
                    .ok_or_else(|| Error::Invalid("message overhead overflow".into()))?,
            )
            .ok_or_else(|| Error::Invalid("token count overflow".into()))
    }
    pub fn into_meter_estimator(self) -> TokenEstimator {
        Rc::new(move |payload| self.estimate_input_tokens(payload).map(Some))
    }
}
impl Default for OpenAiTokenEstimator {
    fn default() -> Self {
        Self::new(None, 3)
    }
}
pub fn fallback_encoding_name(model: Option<&str>) -> &'static str {
    let model = model
        .unwrap_or("")
        .rsplit(':')
        .next()
        .unwrap_or("")
        .to_lowercase();
    if [
        "gpt-5",
        "gpt-4.5",
        "gpt-4.1",
        "gpt-4o",
        "chatgpt-4o",
        "o1",
        "o3",
        "o4-mini",
    ]
    .iter()
    .any(|prefix| model.starts_with(prefix))
    {
        "o200k_base"
    } else {
        "cl100k_base"
    }
}
