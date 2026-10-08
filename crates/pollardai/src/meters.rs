//! Native meter implementations matching Pollard's charge and estimate contract.
use crate::{Error, NodeKind, Result, Value};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use std::{collections::BTreeMap, rc::Rc};

/// An expected governance decision from a custom meter's precheck. Configuration
/// and programming errors should use ordinary `Error` variants instead.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MeterPrecheckRefusal {
    pub reason: String,
    pub detail: String,
    pub audit_meta: Value,
    pub requested: Option<String>,
    pub remaining: Option<String>,
}
impl MeterPrecheckRefusal {
    pub fn new(reason: impl Into<String>) -> Result<Self> {
        let reason = reason.into();
        let refusal = Self {
            detail: reason.clone(),
            reason,
            audit_meta: crate::json!({}),
            requested: None,
            remaining: None,
        };
        refusal.validate()?;
        Ok(refusal)
    }
    pub fn validate(&self) -> Result<()> {
        if self.reason.is_empty() || self.detail.is_empty() {
            return Err(Error::Invalid(
                "meter refusal reason and detail must be nonempty".into(),
            ));
        }
        let object = self.audit_meta.as_object().ok_or_else(|| {
            Error::Invalid("meter refusal audit metadata must be an object".into())
        })?;
        for key in [
            "accounting_fallbacks",
            "avoided",
            "charges",
            "created_at",
            "duration_s",
            "reservation_id",
            "reservation_lease",
            "settlement",
            "usage",
            "state",
            "created_at_unix",
            "window_events",
            "accounting_unknown",
        ] {
            if object.contains_key(key) {
                return Err(Error::Invalid(format!(
                    "meter refusal cannot overwrite runtime metadata: {key}"
                )));
            }
        }
        for amount in [&self.requested, &self.remaining].into_iter().flatten() {
            crate::parse_decimal_exact(amount).map_err(|e| {
                Error::Invalid(format!("meter refusal amount must be finite decimal: {e}"))
            })?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub struct WindowLimit {
    pub limit: f64,
    pub seconds: f64,
}

/// One live call's measurement context. Runtime starts contexts after governance
/// gates and finishes them in reverse order, including cancellation and unwind.
/// Readings are merged into recording metadata before meters settle charges.
pub trait MeterMeasurement {
    fn start(&mut self) -> Result<()> {
        Ok(())
    }
    fn finish(&mut self, _error: Option<&Error>) -> Result<()> {
        Ok(())
    }
    fn readings(&self) -> Result<Value> {
        Ok(crate::json!({}))
    }
}

/// A custom resource meter. Estimates gate dispatch; charges settle actual work.
pub trait Meter {
    fn name(&self) -> &str;
    fn precheck_is_estimate(&self) -> bool {
        false
    }
    fn estimate(&self, _kind: NodeKind, _payload: &Value) -> Result<Option<f64>> {
        Ok(None)
    }
    fn charge(&self, kind: NodeKind, payload: &Value, result: &Value, meta: &Value) -> Result<f64>;
    fn measure(&self) -> Result<Option<Box<dyn MeterMeasurement>>> {
        Ok(None)
    }
    /// Settlement hook for meters that also attach usage, pricing or advice.
    fn charge_with_meta(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        self.charge(kind, payload, result, meta)
    }
    /// Explain why a zero charge should retain an available preflight estimate.
    fn precheck_fallback_reason(
        &self,
        _kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        _meta: &Value,
    ) -> Option<String> {
        None
    }
    fn window(&self) -> Option<WindowLimit> {
        None
    }
    fn window_ledger_key(&self, root_id: &str) -> Result<Option<String>> {
        self.window()
            .map(|window| {
                let limit = crate::result_text_and_digest(&crate::json!(window.limit))?.0;
                window_key(
                    root_id,
                    self.name(),
                    &python_decimal_string(&limit)?,
                    window.seconds,
                )
            })
            .transpose()
    }
}

fn is_call(kind: NodeKind) -> bool {
    matches!(kind, NodeKind::ModelCall | NodeKind::ToolCall)
}

#[derive(Debug, Default)]
pub struct StepMeter;
impl Meter for StepMeter {
    fn name(&self) -> &str {
        "steps"
    }
    fn estimate(&self, kind: NodeKind, _payload: &Value) -> Result<Option<f64>> {
        Ok(Some(f64::from(u8::from(is_call(kind)))))
    }
    fn charge(
        &self,
        kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        _meta: &Value,
    ) -> Result<f64> {
        Ok(f64::from(u8::from(is_call(kind))))
    }
}

#[derive(Debug, Default)]
pub struct DepthMeter;
impl Meter for DepthMeter {
    fn name(&self) -> &str {
        "depth"
    }
    fn charge(
        &self,
        _kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        _meta: &Value,
    ) -> Result<f64> {
        Ok(0.0)
    }
}

#[derive(Debug, Default)]
pub struct WallClockMeter;
impl Meter for WallClockMeter {
    fn name(&self) -> &str {
        "seconds"
    }
    fn charge(
        &self,
        _kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        meta: &Value,
    ) -> Result<f64> {
        Ok(meta
            .get("duration_s")
            .and_then(Value::as_f64)
            .unwrap_or(0.0))
    }
}

pub type TokenEstimator = Rc<dyn Fn(&Value) -> Result<Option<u64>>>;

#[derive(Default)]
pub struct TokenMeter {
    estimator: Option<TokenEstimator>,
    reserved_output_tokens: u64,
}
impl TokenMeter {
    pub fn new(estimator: TokenEstimator, reserved_output_tokens: u64) -> Self {
        Self {
            estimator: Some(estimator),
            reserved_output_tokens,
        }
    }
}
impl Meter for TokenMeter {
    fn name(&self) -> &str {
        "tokens"
    }
    fn precheck_is_estimate(&self) -> bool {
        self.estimator.is_some()
    }
    fn estimate(&self, kind: NodeKind, payload: &Value) -> Result<Option<f64>> {
        if kind != NodeKind::ModelCall {
            return Ok(None);
        }
        self.estimator.as_ref().map_or(Ok(None), |estimate| {
            estimate(payload)?
                .map(|n| {
                    n.checked_add(self.reserved_output_tokens)
                        .map(|n| n as f64)
                        .ok_or_else(|| Error::Invalid("token estimate overflow".into()))
                })
                .transpose()
        })
    }
    fn charge(
        &self,
        kind: NodeKind,
        _payload: &Value,
        result: &Value,
        _meta: &Value,
    ) -> Result<f64> {
        if !is_call(kind) {
            return Ok(0.0);
        }
        let usage = result.get("usage");
        let tokens = usage
            .and_then(|u| u.get("input_tokens"))
            .and_then(Value::as_u64)
            .zip(
                usage
                    .and_then(|u| u.get("output_tokens"))
                    .and_then(Value::as_u64),
            );
        Ok(tokens.and_then(|(i, o)| i.checked_add(o)).unwrap_or(0) as f64)
    }
}

/// Prices are decimal dollars per million tokens, provided by the caller.
#[derive(Debug, Clone)]
pub struct ModelPrice {
    pub input_per_1m: Decimal,
    pub output_per_1m: Decimal,
}
impl ModelPrice {
    pub fn new(input_per_1m: &str, output_per_1m: &str) -> Result<Self> {
        let parse = |s: &str| {
            crate::parse_decimal_exact(s)
                .map_err(|e| Error::Invalid(format!("invalid model price: {e}")))
                .and_then(|n| {
                    if n.is_sign_negative() {
                        Err(Error::Invalid("negative model price".into()))
                    } else {
                        Ok(n)
                    }
                })
        };
        Ok(Self {
            input_per_1m: parse(input_per_1m)?,
            output_per_1m: parse(output_per_1m)?,
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct CostMeter {
    prices: BTreeMap<String, ModelPrice>,
}
impl CostMeter {
    pub fn new(prices: BTreeMap<String, ModelPrice>) -> Self {
        Self { prices }
    }
    pub fn charge_decimal(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
    ) -> Result<Decimal> {
        if kind != NodeKind::ModelCall {
            return Ok(Decimal::ZERO);
        }
        let Some(price) = payload
            .get("model")
            .and_then(Value::as_str)
            .and_then(|m| self.prices.get(m))
        else {
            return Ok(Decimal::ZERO);
        };
        let Some(usage) = result.get("usage").and_then(Value::as_object) else {
            return Ok(Decimal::ZERO);
        };
        let amount = |key| match usage.get(key) {
            None => Some(0),
            Some(v) => v.as_u64(),
        };
        let Some((input, output)) = amount("input_tokens").zip(amount("output_tokens")) else {
            return Ok(Decimal::ZERO);
        };
        crate::decimal::cost_per_million(&[
            (input, price.input_per_1m),
            (output, price.output_per_1m),
        ])
        .ok_or_else(|| {
            Error::Invalid("cost cannot be represented exactly as a native decimal".into())
        })
    }
}
impl Meter for CostMeter {
    fn name(&self) -> &str {
        "usd"
    }
    fn charge(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        _meta: &Value,
    ) -> Result<f64> {
        self.charge_decimal(kind, payload, result)?
            .to_f64()
            .ok_or_else(|| Error::Invalid("cost out of range".into()))
    }
}

/// A meter-backed sliding-window limit. The runtime keeps its ledger in records.
pub struct WindowMeter {
    name: String,
    limits: WindowLimit,
    meter: Rc<dyn Meter>,
    ledger_limit: String,
}
impl WindowMeter {
    pub fn new(
        name: impl Into<String>,
        limit: f64,
        seconds: f64,
        meter: Option<Rc<dyn Meter>>,
    ) -> Result<Self> {
        let name = name.into();
        if name.is_empty()
            || !limit.is_finite()
            || limit <= 0.0
            || !seconds.is_finite()
            || seconds <= 0.0
        {
            return Err(Error::Invalid(
                "window name, positive finite limit and seconds are required".into(),
            ));
        }
        let meter = meter.unwrap_or_else(|| {
            if name == "tokens" {
                Rc::new(TokenMeter::default())
            } else {
                Rc::new(StepMeter)
            }
        });
        let limit_text = crate::result_text_and_digest(&crate::json!(limit))?.0;
        crate::parse_decimal_exact(&limit_text)
            .map_err(|e| Error::Invalid(format!("invalid window limit: {e}")))?;
        let ledger_limit = python_decimal_string(&limit_text)?;
        Ok(Self {
            name,
            limits: WindowLimit { limit, seconds },
            meter,
            ledger_limit,
        })
    }
    /// Preserve the Python Decimal spelling used in the window ledger identity.
    /// Use `"3"` to share Python `WindowMeter(..., 3, ...)`; `new(..., 3.0, ...)`
    /// matches Python's distinct float limit `3.0`.
    pub fn new_decimal(
        name: impl Into<String>,
        limit: &str,
        seconds: f64,
        meter: Option<Rc<dyn Meter>>,
    ) -> Result<Self> {
        let amount = crate::parse_decimal_exact(limit)
            .map_err(|e| Error::Invalid(format!("invalid window limit: {e}")))?;
        let mut window = Self::new(
            name,
            amount
                .to_f64()
                .ok_or_else(|| Error::Invalid("window limit out of range".into()))?,
            seconds,
            meter,
        )?;
        window.ledger_limit = python_decimal_string(limit)?;
        Ok(window)
    }
}
impl Meter for WindowMeter {
    fn name(&self) -> &str {
        &self.name
    }
    fn precheck_is_estimate(&self) -> bool {
        self.meter.precheck_is_estimate()
    }
    fn estimate(&self, kind: NodeKind, payload: &Value) -> Result<Option<f64>> {
        self.meter.estimate(kind, payload)
    }
    fn charge(&self, kind: NodeKind, payload: &Value, result: &Value, meta: &Value) -> Result<f64> {
        self.meter.charge(kind, payload, result, meta)
    }
    fn measure(&self) -> Result<Option<Box<dyn MeterMeasurement>>> {
        self.meter.measure()
    }
    fn charge_with_meta(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        self.meter.charge_with_meta(kind, payload, result, meta)
    }
    fn precheck_fallback_reason(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &Value,
    ) -> Option<String> {
        self.meter
            .precheck_fallback_reason(kind, payload, result, meta)
    }
    fn window(&self) -> Option<WindowLimit> {
        Some(self.limits)
    }
    fn window_ledger_key(&self, root_id: &str) -> Result<Option<String>> {
        window_key(root_id, &self.name, &self.ledger_limit, self.limits.seconds).map(Some)
    }
}

fn window_key(root_id: &str, name: &str, limit: &str, seconds: f64) -> Result<String> {
    let seconds = crate::result_text_and_digest(&crate::json!(seconds))?.0;
    let document =
        crate::json!({"root_id":root_id,"name":name,"limit":limit,"window_seconds":seconds});
    Ok(crate::identity::hash(
        b"",
        &crate::canonical_bytes(&document)?,
    ))
}

// Decimal string formatting preserves significant trailing zeroes and explicit
// positive exponents, unlike float formatting or Decimal::normalize().
pub(crate) fn python_decimal_string(input: &str) -> Result<String> {
    let input = input.trim();
    let input = input.strip_prefix('+').unwrap_or(input);
    let (negative, input) = input
        .strip_prefix('-')
        .map_or((false, input), |v| (true, v));
    let mut parts = input.split(['e', 'E']);
    let coefficient = parts.next().unwrap_or("");
    let exponent = parts
        .next()
        .map(|s| s.parse::<i32>())
        .transpose()
        .map_err(|_| Error::Invalid("invalid decimal exponent".into()))?
        .unwrap_or(0);
    if parts.next().is_some() {
        return Err(Error::Invalid("invalid decimal".into()));
    }
    let mut pieces = coefficient.split('.');
    let whole = pieces.next().unwrap_or("");
    let fraction = pieces.next().unwrap_or("");
    if pieces.next().is_some()
        || (whole.is_empty() && fraction.is_empty())
        || !whole
            .bytes()
            .chain(fraction.bytes())
            .all(|b| b.is_ascii_digit())
    {
        return Err(Error::Invalid("invalid decimal".into()));
    }
    let digits = format!("{whole}{fraction}");
    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let exponent = exponent
        .checked_sub(fraction.len() as i32)
        .ok_or_else(|| Error::Invalid("decimal exponent overflow".into()))?;
    let adjusted = exponent
        .checked_add(digits.len() as i32 - 1)
        .ok_or_else(|| Error::Invalid("decimal exponent overflow".into()))?;
    let sign = if negative { "-" } else { "" };
    if exponent <= 0 && adjusted >= -6 {
        let point = digits.len() as i32 + exponent;
        if point <= 0 {
            Ok(format!("{sign}0.{}{digits}", "0".repeat((-point) as usize)))
        } else if exponent == 0 {
            Ok(format!("{sign}{digits}"))
        } else {
            let point = point as usize;
            Ok(format!("{sign}{}.{}", &digits[..point], &digits[point..]))
        }
    } else {
        let tail = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        Ok(format!("{sign}{}{tail}E{adjusted:+}", &digits[..1]))
    }
}

/// Charge a numeric measurement supplied in node metadata, e.g. `joules`.
#[derive(Debug, Clone)]
pub struct MetadataMeter {
    name: String,
}
impl MetadataMeter {
    pub fn new(name: impl Into<String>) -> Result<Self> {
        let name = name.into();
        if name.is_empty() {
            return Err(Error::Invalid("meter name is empty".into()));
        }
        Ok(Self { name })
    }
}
impl Meter for MetadataMeter {
    fn name(&self) -> &str {
        &self.name
    }
    fn charge(
        &self,
        _kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        meta: &Value,
    ) -> Result<f64> {
        Ok(meta.get(&self.name).and_then(Value::as_f64).unwrap_or(0.0))
    }
}

/// Trapezoidal integration of `(monotonic seconds, watts)` samples.
pub fn integrate_energy(samples: &[(f64, f64)]) -> Result<f64> {
    if samples
        .iter()
        .any(|(t, w)| !t.is_finite() || !w.is_finite() || *w < 0.0)
    {
        return Err(Error::Invalid(
            "energy samples must be finite with nonnegative power".into(),
        ));
    }
    samples.windows(2).try_fold(0.0, |total, pair| {
        let ((t0, w0), (t1, w1)) = (pair[0], pair[1]);
        if t1 < t0 {
            return Err(Error::Invalid("energy timestamps must be monotonic".into()));
        }
        let total = total + (t1 - t0) * (w0 + w1) / 2.0;
        if !total.is_finite() {
            return Err(Error::Invalid("energy integral overflow".into()));
        }
        Ok(total)
    })
}
