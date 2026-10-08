use crate::{Error, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// JavaScript's exact integer bound, retained for metering compatibility.
/// Identity payloads accept arbitrary-size integers, as Python Pollard does.
pub const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub(crate) fn hash(prefix: &[u8], bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(prefix);
    h.update(bytes);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut text = String::with_capacity(64);
    for byte in h.finalize() {
        text.push(HEX[(byte >> 4) as usize] as char);
        text.push(HEX[(byte & 15) as usize] as char);
    }
    text
}

/// Compact UTF-8 JSON with Unicode scalar sorted keys; floats are rejected.
pub fn canonical_bytes(value: &Value) -> Result<Vec<u8>> {
    let mut out = Vec::with_capacity(128);
    if let Err(error) = write(value, &mut out, true) {
        // Python reports the first invalid leaf in caller insertion order, then
        // sorts keys for encoding. Only traverse again on the error path.
        if let Some(path) = float_path(value, &mut "$".to_owned()) {
            return Err(Error::Invalid(format!(
                "floats are not allowed in identity payloads at {path}"
            )));
        }
        return Err(error);
    }
    Ok(out)
}

fn float_path(value: &Value, path: &mut String) -> Option<String> {
    match value {
        Value::Number(_) if integer_text(value).is_none() => return Some(path.clone()),
        Value::Object(obj) => {
            for (key, child) in obj {
                let length = path.len();
                path.push('.');
                path.push_str(key);
                if let Some(found) = float_path(child, path) {
                    return Some(found);
                }
                path.truncate(length);
            }
        }
        Value::Array(arr) => {
            for (i, child) in arr.iter().enumerate() {
                let length = path.len();
                path.push_str(&format!("[{i}]"));
                if let Some(found) = float_path(child, path) {
                    return Some(found);
                }
                path.truncate(length);
            }
        }
        _ => {}
    }
    None
}

fn write(value: &Value, out: &mut Vec<u8>, identity: bool) -> Result<()> {
    match value {
        Value::Object(obj) => {
            out.push(b'{');
            let mut entries: Vec<_> = obj.iter().collect();
            entries.sort_unstable_by_key(|(key, _)| *key);
            for (index, (key, value)) in entries.into_iter().enumerate() {
                if index != 0 {
                    out.push(b',');
                }
                serde_json::to_writer(&mut *out, key).map_err(|e| Error::Invalid(e.to_string()))?;
                out.push(b':');
                write(value, out, identity)?;
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
        Value::Number(_) if identity => {
            let integer = integer_text(value).ok_or_else(|| {
                Error::Invalid("floats are not allowed in identity payloads".into())
            })?;
            out.extend(integer.as_bytes());
        }
        Value::Number(number) => {
            if let Some(integer) = integer_text(value) {
                out.extend(integer.as_bytes());
            } else {
                let float = number
                    .as_f64()
                    .filter(|n| n.is_finite())
                    .ok_or_else(|| Error::Invalid("result float must be finite".into()))?;
                out.extend(python_float_text(float).as_bytes());
            }
        }
        _ => serde_json::to_writer(out, value).map_err(|e| Error::Invalid(e.to_string()))?,
    }
    Ok(())
}

/// Compute an identity using Pollard's frozen v1 domain.
pub fn node_id(kind: &str, parent: Option<&str>, attempt: u64, payload: &Value) -> Result<String> {
    // The envelope's canonical key order is fixed. Encode borrowed values
    // directly instead of cloning the complete payload into a temporary object.
    let mut out = Vec::with_capacity(192);
    out.extend_from_slice(b"{\"a\":");
    serde_json::to_writer(&mut out, &attempt).map_err(|e| Error::Invalid(e.to_string()))?;
    out.extend_from_slice(b",\"k\":");
    serde_json::to_writer(&mut out, kind).map_err(|e| Error::Invalid(e.to_string()))?;
    out.extend_from_slice(b",\"p\":");
    serde_json::to_writer(&mut out, parent.unwrap_or(""))
        .map_err(|e| Error::Invalid(e.to_string()))?;
    out.extend_from_slice(b",\"pl\":");
    if let Err(error) = write(payload, &mut out, true) {
        if let Some(path) = float_path(payload, &mut "$.pl".to_owned()) {
            return Err(Error::Invalid(format!(
                "floats are not allowed in identity payloads at {path}"
            )));
        }
        return Err(error);
    }
    out.push(b'}');
    Ok(hash(b"pollard/v1\n", &out))
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

// Python json.dumps uses float repr: fixed notation for decimal exponents
// -4..15, an explicit exponent sign, and at least two exponent digits.
fn python_float_text(value: f64) -> String {
    let repr = serde_json::Number::from_f64(value)
        .expect("finite float")
        .to_string();
    let negative = repr.starts_with('-');
    let unsigned = repr.trim_start_matches('-');
    let (coefficient, exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, 0), |(c, e)| {
            (c, e.parse::<i32>().expect("float exponent"))
        });
    let mut position = coefficient.find('.').unwrap_or(coefficient.len()) as i32 + exponent;
    let mut digits = coefficient.replace('.', "");
    let leading = digits.bytes().take_while(|b| *b == b'0').count();
    if leading == digits.len() {
        return if negative {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    digits.drain(..leading);
    position -= leading as i32;
    while digits.len() > 1 && digits.ends_with('0') {
        digits.pop();
    }
    let sign = if negative { "-" } else { "" };
    let scientific_exponent = position - 1;
    if !(-4..16).contains(&scientific_exponent) {
        let tail = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        format!(
            "{sign}{}{tail}e{}{exponent:02}",
            &digits[..1],
            if scientific_exponent < 0 { "-" } else { "+" },
            exponent = scientific_exponent.unsigned_abs()
        )
    } else if position <= 0 {
        format!("{sign}0.{}{digits}", "0".repeat((-position) as usize))
    } else if position as usize >= digits.len() {
        format!(
            "{sign}{digits}{}.0",
            "0".repeat(position as usize - digits.len())
        )
    } else {
        format!(
            "{sign}{}.{}",
            &digits[..position as usize],
            &digits[position as usize..]
        )
    }
}

/// A deterministic content commitment. This is not encryption.
pub fn redact(value: &Value, hint: Option<&str>) -> Result<Value> {
    Ok(
        json!({"__pollard_redacted":hash(b"pollard/v1:redact\n", &canonical_bytes(value)?),"hint":hint}),
    )
}

/// Recognize the exact marker shape used by Python Pollard 1.6.0.
pub fn is_redacted(value: &Value) -> bool {
    value.as_object().is_some_and(|obj| {
        obj.len() == 2
            && obj
                .get("__pollard_redacted")
                .and_then(Value::as_str)
                .is_some_and(hex64)
            && obj
                .get("hint")
                .is_some_and(|v| v.is_null() || v.is_string())
    })
}

/// Check nested objects and arrays for an exact Pollard redaction marker.
pub fn contains_redaction(value: &Value) -> bool {
    is_redacted(value)
        || match value {
            Value::Object(obj) => obj.values().any(contains_redaction),
            Value::Array(arr) => arr.iter().any(contains_redaction),
            _ => false,
        }
}

pub(crate) fn integer_text(value: &Value) -> Option<String> {
    let text = value.as_number()?.to_string();
    let digits = text.strip_prefix('-').unwrap_or(&text);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    // JSON permits -0; Python's integer parser canonicalizes it to zero.
    Some(if text == "-0" { "0".into() } else { text })
}

pub(crate) fn compare_integers(left: &Value, right: &Value) -> Option<std::cmp::Ordering> {
    let left = integer_text(left)?;
    let right = integer_text(right)?;
    let (ln, rn) = (left.starts_with('-'), right.starts_with('-'));
    if ln != rn {
        return Some(if ln {
            std::cmp::Ordering::Less
        } else {
            std::cmp::Ordering::Greater
        });
    }
    let (l, r) = (left.trim_start_matches('-'), right.trim_start_matches('-'));
    let order = l.len().cmp(&r.len()).then_with(|| l.cmp(r));
    Some(if ln { order.reverse() } else { order })
}

/// Compare the parsed JSON value with a native result without mistaking float
/// exponent spelling for a content change. Integer/float types and signed zero
/// remain distinct; the separate raw text digest still commits exact bytes.
pub(crate) fn result_values_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(_), Value::Number(_)) => match (integer_text(left), integer_text(right)) {
            (Some(left), Some(right)) => left == right,
            (None, None) => match (left.as_f64(), right.as_f64()) {
                (Some(left), Some(right)) if left.is_finite() && right.is_finite() => {
                    left.to_bits() == right.to_bits()
                }
                _ => false,
            },
            _ => false,
        },
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .is_some_and(|other| result_values_equal(value, other))
                })
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| result_values_equal(left, right))
        }
        _ => left == right,
    }
}

/// serde_json's arbitrary-precision parser preserves overflowing exponent
/// spellings. Python's JSON value contract only admits finite floating values.
pub(crate) fn validate_finite_json(value: &Value) -> Result<()> {
    match value {
        Value::Number(_) if integer_text(value).is_none() => {
            if !value.as_f64().is_some_and(f64::is_finite) {
                return Err(Error::Invalid("JSON floating values must be finite".into()));
            }
        }
        Value::Object(obj) => {
            for child in obj.values() {
                validate_finite_json(child)?;
            }
        }
        Value::Array(arr) => {
            for child in arr {
                validate_finite_json(child)?;
            }
        }
        _ => {}
    }
    Ok(())
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
