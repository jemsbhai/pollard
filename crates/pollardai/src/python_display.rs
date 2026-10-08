//! Python `str`/`repr` for JSON values used by human-readable release surfaces.
//! Unicode classification is pinned to the Python 3.12 oracle, not Rust's
//! toolchain-specific Unicode version. It never changes stored JSON or identity.
use crate::Value;
use std::fmt::Write;

include!("python_printable.rs");

pub(crate) fn display(value: &Value) -> String {
    if let Value::String(value) = value {
        value.clone()
    } else {
        repr(value)
    }
}

fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(_) => crate::result_text_and_digest(value)
            .map(|(text, _)| text)
            .unwrap_or_else(|_| value.to_string()),
        Value::String(value) => quoted(value),
        Value::Array(values) => format!(
            "[{}]",
            values.iter().map(repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!("{}: {}", quoted(key), repr(value)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn quoted(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(value.len() + 2);
    out.push(quote);
    for character in value.chars() {
        match character {
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            '\n' => out.push_str("\\n"),
            '\\' => out.push_str("\\\\"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => {
                let code = u32::from(c);
                let printable = NONPRINTABLE
                    .binary_search_by(|&(first, last)| {
                        if code < first {
                            std::cmp::Ordering::Greater
                        } else if code > last {
                            std::cmp::Ordering::Less
                        } else {
                            std::cmp::Ordering::Equal
                        }
                    })
                    .is_err();
                if printable {
                    out.push(c);
                } else if code <= 0xff {
                    write!(&mut out, "\\x{code:02x}").expect("String write");
                } else if code <= 0xffff {
                    write!(&mut out, "\\u{code:04x}").expect("String write");
                } else {
                    write!(&mut out, "\\U{code:08x}").expect("String write");
                }
            }
        }
    }
    out.push(quote);
    out
}
