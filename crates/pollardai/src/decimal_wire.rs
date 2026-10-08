//! Decimal text for cross-language reservation retries.
//!
//! A native Decimal cannot retain a positive exponent or every signed-zero
//! spelling. These additive inputs retain Python's `str(Decimal(text))` form
//! for fingerprints while validating that every amount fits native accounting.
use crate::{json, BudgetReservation, Error, Result, Value, WindowReservation};
use rust_decimal::Decimal;
use std::collections::BTreeMap;

/// Cumulative reservation with decimal amounts supplied as text.
#[derive(Debug, Clone, Default)]
pub struct TextBudgetReservation {
    pub scope_id: String,
    pub limits: BTreeMap<String, String>,
    pub baseline: BTreeMap<String, String>,
    pub estimates: BTreeMap<String, String>,
}

/// Sliding-window reservation retaining the original decimal representation.
#[derive(Debug, Clone)]
pub struct TextWindowReservation {
    pub ledger_key: String,
    pub meter: String,
    pub limit: String,
    pub amount: String,
    pub window_seconds: f64,
}

pub(crate) struct PreparedReservation {
    pub budgets: Vec<BudgetReservation>,
    pub windows: Vec<WindowReservation>,
    pub document: Value,
    pub encoded: (String, String),
}
pub(crate) struct PreparedCharges {
    pub amounts: BTreeMap<String, Decimal>,
    pub document: Value,
    pub encoded: (String, String),
}
fn amount(text: &str) -> Result<(Decimal, String)> {
    let value = crate::parse_decimal_exact(text.trim())
        .map_err(|e| Error::Invalid(format!("invalid exact reservation amount: {e}")))?;
    if value < Decimal::ZERO {
        return Err(Error::Invalid(
            "reservation amounts must be nonnegative".into(),
        ));
    }
    Ok((value, crate::meters::python_decimal_string(text)?))
}
fn amounts(values: &BTreeMap<String, String>) -> Result<(BTreeMap<String, Decimal>, Value)> {
    let mut numeric = BTreeMap::new();
    let mut text = BTreeMap::new();
    for (key, value) in values {
        let (number, spelling) = amount(value)?;
        numeric.insert(key.clone(), number);
        text.insert(key.clone(), spelling);
    }
    Ok((numeric, json!(text)))
}
pub(crate) fn prepare_request(
    budgets: &[TextBudgetReservation],
    windows: &[TextWindowReservation],
    lease: f64,
) -> Result<PreparedReservation> {
    let mut numeric_budgets = Vec::new();
    let mut budget_documents = Vec::new();
    let mut ordered: Vec<_> = budgets.iter().collect();
    ordered.sort_by(|a, b| a.scope_id.cmp(&b.scope_id));
    for b in ordered {
        let (limits, limit_text) = amounts(&b.limits)?;
        let (baseline, baseline_text) = amounts(&b.baseline)?;
        let (estimates, estimate_text) = amounts(&b.estimates)?;
        numeric_budgets.push(BudgetReservation {
            scope_id: b.scope_id.clone(),
            limits,
            baseline,
            estimates,
        });
        budget_documents.push(json!({"scope_id":b.scope_id,"limits":limit_text,"baseline":baseline_text,"estimates":estimate_text}));
    }
    let mut numeric_windows = Vec::new();
    let mut window_documents = Vec::new();
    let mut ordered: Vec<_> = windows.iter().collect();
    ordered.sort_by(|a, b| a.ledger_key.cmp(&b.ledger_key));
    for w in ordered {
        let (limit, limit_text) = amount(&w.limit)?;
        let (amount, amount_text) = amount(&w.amount)?;
        numeric_windows.push(WindowReservation {
            ledger_key: w.ledger_key.clone(),
            meter: w.meter.clone(),
            limit,
            amount,
            window_seconds: w.window_seconds,
        });
        window_documents.push(json!({"ledger_key":w.ledger_key,"meter":w.meter,"limit":limit_text,"amount":amount_text,"window_seconds":crate::result_text_and_digest(&json!(w.window_seconds))?.0}));
    }
    // Apply the same numeric validation as native reservations, then replace
    // their lossy Decimal renderings with the validated original spellings.
    crate::kv::reservation_request(&numeric_budgets, &numeric_windows, lease)?;
    let document = json!({"budgets":budget_documents,"windows":window_documents,"lease_seconds":crate::result_text_and_digest(&json!(lease))?.0});
    let encoded = crate::kv::digest_document(&document)?;
    Ok(PreparedReservation {
        budgets: numeric_budgets,
        windows: numeric_windows,
        document,
        encoded,
    })
}
pub(crate) fn prepare_charges(charges: &BTreeMap<String, String>) -> Result<PreparedCharges> {
    let (amounts, document) = amounts(charges)?;
    let encoded = crate::kv::digest_document(&document)?;
    Ok(PreparedCharges {
        amounts,
        document,
        encoded,
    })
}

/// Canonical Python-compatible request text and digest without database I/O.
pub fn reservation_request_text(
    budgets: &[TextBudgetReservation],
    windows: &[TextWindowReservation],
    lease: f64,
) -> Result<(String, String)> {
    Ok(prepare_request(budgets, windows, lease)?.encoded)
}
/// Canonical Python-compatible settlement text and digest without database I/O.
pub fn reservation_charges_text(charges: &BTreeMap<String, String>) -> Result<(String, String)> {
    Ok(prepare_charges(charges)?.encoded)
}

pub(crate) fn budget_spelling<'a>(
    document: &'a Value,
    scope: &str,
    meter: &str,
    field: &str,
) -> Option<&'a str> {
    document["budgets"]
        .as_array()?
        .iter()
        .find(|b| b["scope_id"].as_str() == Some(scope))?[field]
        .get(meter)?
        .as_str()
}
pub(crate) fn window_spelling<'a>(
    document: &'a Value,
    ledger: &str,
    field: &str,
) -> Option<&'a str> {
    document["windows"]
        .as_array()?
        .iter()
        .find(|w| w["ledger_key"].as_str() == Some(ledger))?[field]
        .as_str()
}

/// Preserve the preferred Python exponent when adding persisted ledger text.
/// Native amounts are bounded, so nonzero significant results must still fit.
pub(crate) fn add_text(left: &str, right: &str) -> Result<String> {
    let parse = |text: &str| {
        crate::parse_decimal_exact(text)
            .map_err(|e| Error::Integrity(format!("invalid decimal ledger: {e}")))
    };
    let value = crate::decimal::exact_add(parse(left)?, parse(right)?).ok_or_else(|| {
        Error::Integrity("decimal ledger result is not exactly representable".into())
    })?;
    let exponent = |text: &str| -> Result<i32> {
        let text = crate::meters::python_decimal_string(text)?;
        let (coefficient, exponent) = text.split_once('E').map_or((text.as_str(), 0), |(c, e)| {
            (c, e.parse::<i32>().expect("validated exponent"))
        });
        Ok(exponent
            - coefficient
                .split_once('.')
                .map_or(0, |(_, f)| f.len() as i32))
    };
    let preferred = exponent(left)?.min(exponent(right)?);
    if value.is_zero() {
        let negative = left.starts_with('-') && right.starts_with('-');
        return crate::meters::python_decimal_string(&format!(
            "{}0E{preferred:+}",
            if negative { "-" } else { "" }
        ));
    }
    let mut text = value.to_string();
    if preferred > 0 {
        // Both operands have this many trailing integral zeroes; an exact sum
        // retains them. Keep the exponent instead of expanding it to zeroes.
        let trim = preferred as usize;
        if text.contains('.') || !text.ends_with(&"0".repeat(trim)) {
            return Err(Error::Integrity(
                "inconsistent decimal ledger exponent".into(),
            ));
        }
        text.truncate(text.len() - trim);
        text.push_str(&format!("E+{preferred}"));
    } else {
        // A zero with an extreme exponent must not force unbounded padding.
        // Python's configurable context is not emulated for such ledgers.
        let scale = preferred
            .checked_neg()
            .filter(|s| *s <= 28)
            .ok_or_else(|| {
                Error::Integrity("ledger preferred scale exceeds native decimal range".into())
            })? as u32;
        if scale > value.scale() {
            if !text.contains('.') {
                text.push('.');
            }
            text.push_str(&"0".repeat((scale - value.scale()) as usize));
        }
    }
    crate::meters::python_decimal_string(&text)
}
