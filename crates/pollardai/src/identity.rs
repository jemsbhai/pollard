use crate::{Error, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// Maximum portable integer shared with JavaScript's exact integer range.
pub const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub(crate) fn hash(prefix: &[u8], bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(prefix);
    h.update(bytes);
    format!("{:x}", h.finalize())
}

/// Compact UTF-8 JSON with Unicode scalar sorted keys; floats are rejected.
pub fn canonical_bytes(value: &Value) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    write(value, &mut out, true)?;
    Ok(out)
}

fn write(value: &Value, out: &mut Vec<u8>, identity: bool) -> Result<()> {
    match value {
        Value::Object(obj) => {
            out.push(b'{');
            let mut keys: Vec<_> = obj.keys().collect();
            keys.sort();
            for (index, key) in keys.into_iter().enumerate() {
                if index != 0 {
                    out.push(b',');
                }
                out.extend(serde_json::to_vec(key).map_err(|e| Error::Invalid(e.to_string()))?);
                out.push(b':');
                write(&obj[key], out, identity)?;
            }
            out.push(b'}');
        }
        Value::Array(values) => {
            out.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    out.push(b',');
                }
                write(value, out, identity)?;
            }
            out.push(b']');
        }
        Value::Number(number) if identity => {
            if let Some(n) = number.as_i64() {
                if n.unsigned_abs() > MAX_SAFE_INTEGER {
                    return Err(Error::Invalid("unsafe identity integer".into()));
                }
            } else if let Some(n) = number.as_u64() {
                if n > MAX_SAFE_INTEGER {
                    return Err(Error::Invalid("unsafe identity integer".into()));
                }
            } else {
                return Err(Error::Invalid(
                    "floats are not allowed in identity payloads".into(),
                ));
            }
            out.extend(number.to_string().as_bytes());
        }
        _ => out.extend(serde_json::to_vec(value).map_err(|e| Error::Invalid(e.to_string()))?),
    }
    Ok(())
}

/// Compute an identity using Pollard's frozen v1 domain.
pub fn node_id(kind: &str, parent: Option<&str>, attempt: u64, payload: &Value) -> Result<String> {
    safe_amount(attempt, "attempt")?;
    Ok(hash(
        b"pollard/v1\n",
        &canonical_bytes(&json!({"a":attempt,"k":kind,"p":parent.unwrap_or(""),"pl":payload}))?,
    ))
}

/// SHA-256 of canonical identity bytes, without a domain prefix.
pub fn digest_payload(payload: &Value) -> Result<String> {
    Ok(hash(b"", &canonical_bytes(payload)?))
}

/// Digest exact stored UTF-8 text; imported text must not be reserialized.
pub fn result_digest_from_text(text: &str) -> String {
    hash(b"pollard/v1:result\n", text.as_bytes())
}

/// Serialize a native result once and return its text and integrity digest.
/// Imported Python results should retain their original `result_text` instead.
pub fn result_text_and_digest(result: &Value) -> Result<(String, String)> {
    let mut out = Vec::new();
    write(result, &mut out, false)?;
    let text = String::from_utf8(out).map_err(|e| Error::Invalid(e.to_string()))?;
    let digest = result_digest_from_text(&text);
    Ok((text, digest))
}

/// A deterministic content commitment. This is not encryption.
pub fn redact(value: &Value, hint: Option<&str>) -> Result<Value> {
    Ok(
        json!({"__pollard_redacted":hash(b"pollard/v1:redact\n", &canonical_bytes(value)?),"hint":hint}),
    )
}

pub(crate) fn safe_amount(value: u64, label: &str) -> Result<()> {
    if value > MAX_SAFE_INTEGER {
        return Err(Error::Invalid(format!(
            "{label} exceeds the portable integer range"
        )));
    }
    Ok(())
}

pub(crate) fn hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
