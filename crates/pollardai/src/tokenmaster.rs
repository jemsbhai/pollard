//! Offline profile governance compatible with Pollard 1.6 / Tokenmaster 0.2.
//!
//! Catalogs are explicit caller-owned snapshots; this module never fetches prices.
//! Numeric counters use u64 and prices use the crate's bounded Decimal arithmetic.
//! Algorithms adapted from Tokenmaster (MIT, Copyright 2026 Muntaser Syed).
use crate::{json, Error, Meter, MeterPrecheckRefusal, NodeKind, Result, TokenEstimator, Value};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde::{Deserialize, Serialize};
use std::{cell::RefCell, collections::BTreeMap, rc::Rc};

fn usd() -> String {
    "USD".into()
}
fn user_source() -> String {
    "user".into()
}
fn schema_version() -> String {
    "0.1".into()
}
fn standard() -> String {
    "standard".into()
}
fn global() -> String {
    "global".into()
}
fn basis() -> String {
    "request_input_tokens".into()
}
fn invalid(message: impl Into<String>) -> Error {
    Error::Invalid(message.into())
}
fn wire<T: Serialize>(value: &T) -> Value {
    serde_json::to_value(value).expect("finite validated profile data")
}
fn detail(error: &Error) -> String {
    match error {
        Error::Invalid(s) | Error::Handler(s) | Error::NotFound(s) => s.clone(),
        _ => error.to_string(),
    }
}
const CATEGORIES: [&str; 5] = [
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Pricing {
    #[serde(deserialize_with = "deserialize_price")]
    pub input: f64,
    #[serde(deserialize_with = "deserialize_price")]
    pub output: f64,
    #[serde(default, deserialize_with = "deserialize_price")]
    pub cache_read: f64,
    #[serde(default, deserialize_with = "deserialize_price")]
    pub cache_write: f64,
    #[serde(default = "usd")]
    pub currency: String,
    #[serde(default)]
    pub as_of: Option<String>,
}
fn deserialize_price<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<f64, D::Error> {
    let value = Value::deserialize(deserializer)?;
    let rate = value
        .as_f64()
        .filter(|rate| rate.is_finite())
        .ok_or_else(|| serde::de::Error::custom("pricing rate must be a finite number"))?;
    crate::parse_decimal_exact(&value.to_string()).map_err(serde::de::Error::custom)?;
    Ok(rate)
}
impl Pricing {
    pub fn validate(&self) -> Result<()> {
        for rate in [self.input, self.output, self.cache_read, self.cache_write] {
            if !rate.is_finite() || rate < 0.0 {
                return Err(invalid("pricing rates must be finite and nonnegative"));
            }
            crate::parse_decimal_exact(&rate.to_string())
                .map_err(|e| invalid(format!("pricing rate outside exact decimal range: {e}")))?;
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CalibrationRecord {
    pub model_id: String,
    pub effective_context: u64,
    pub method: String,
    pub source: String,
    #[serde(default)]
    pub measured_at: Option<String>,
    #[serde(default)]
    pub confidence: Option<String>,
    #[serde(default = "schema_version")]
    pub schema_version: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelProfile {
    pub model_id: String,
    pub provider: String,
    pub window_nominal: u64,
    #[serde(default)]
    pub max_output: Option<u64>,
    #[serde(default)]
    pub pricing: Option<Pricing>,
    #[serde(default)]
    pub tokenizer_hint: Option<String>,
    #[serde(default)]
    pub effective: Option<CalibrationRecord>,
    #[serde(default = "user_source")]
    pub source: String,
    #[serde(default = "schema_version")]
    pub schema_version: String,
}
impl ModelProfile {
    pub fn from_value(value: Value) -> Result<Self> {
        let profile: Self = serde_json::from_value(value).map_err(|e| invalid(e.to_string()))?;
        profile.validate()?;
        Ok(profile)
    }
    pub fn validate(&self) -> Result<()> {
        if self.window_nominal == 0
            || self
                .effective
                .as_ref()
                .is_some_and(|e| e.effective_context == 0)
        {
            return Err(invalid("profile capacities must be positive"));
        }
        if let Some(pricing) = &self.pricing {
            pricing.validate()?;
        }
        Ok(())
    }
    pub fn window_effective(&self) -> u64 {
        self.effective
            .as_ref()
            .map_or(self.window_nominal, |e| e.effective_context)
    }
    pub fn effective_source(&self) -> String {
        self.effective.as_ref().map_or_else(
            || "nominal (uncalibrated)".into(),
            |e| format!("calibration:{} ({})", e.method, e.source),
        )
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PricingScope {
    #[serde(default = "standard")]
    pub service_tier: String,
    #[serde(default = "global")]
    pub region: String,
    #[serde(default = "basis")]
    pub basis: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unpriced_usage_categories: Vec<String>,
}
impl Default for PricingScope {
    fn default() -> Self {
        Self {
            service_tier: standard(),
            region: global(),
            basis: basis(),
            unpriced_usage_categories: Vec::new(),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PricingTier {
    pub min_input_tokens: u64,
    pub pricing: Pricing,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PricingSchedule {
    pub base: Pricing,
    #[serde(default)]
    pub tiers: Vec<PricingTier>,
    #[serde(default)]
    pub scope: PricingScope,
}
impl PricingSchedule {
    pub fn validate(&self) -> Result<()> {
        self.base.validate()?;
        if self.scope.basis != basis()
            || self.scope.region.trim().is_empty()
            || self.scope.service_tier.trim().is_empty()
        {
            return Err(invalid("unsupported or empty pricing scope"));
        }
        let mut seen = std::collections::BTreeSet::new();
        for category in &self.scope.unpriced_usage_categories {
            if !CATEGORIES.contains(&category.as_str()) || !seen.insert(category) {
                return Err(invalid("invalid or duplicate unpriced usage category"));
            }
        }
        let mut thresholds = std::collections::BTreeSet::new();
        for tier in &self.tiers {
            tier.pricing.validate()?;
            if tier.pricing.currency != self.base.currency
                || !thresholds.insert(tier.min_input_tokens)
            {
                return Err(invalid("duplicate threshold or inconsistent tier currency"));
            }
        }
        Ok(())
    }
    pub fn price_for(&self, input: u64) -> Result<(&Pricing, Option<u64>)> {
        self.validate()?;
        Ok(self
            .tiers
            .iter()
            .filter(|t| t.min_input_tokens <= input)
            .max_by_key(|t| t.min_input_tokens)
            .map_or((&self.base, None), |t| {
                (&t.pricing, Some(t.min_input_tokens))
            }))
    }
}

/// Caller-supplied offline profiles, aliases, and dated pricing schedules.
#[derive(Debug, Clone, Default)]
pub struct ProfileRegistry {
    pub snapshot_date: Option<String>,
    profiles: BTreeMap<String, ModelProfile>,
    aliases: BTreeMap<String, String>,
    schedules: BTreeMap<String, PricingSchedule>,
}
fn norm(value: &str) -> String {
    value.trim().to_lowercase()
}
impl ProfileRegistry {
    pub fn from_value(value: &Value) -> Result<Self> {
        let object = value
            .as_object()
            .ok_or_else(|| invalid("profile catalog must be an object"))?;
        let mut registry = Self {
            snapshot_date: object
                .get("snapshot_date")
                .and_then(Value::as_str)
                .map(str::to_owned),
            ..Self::default()
        };
        let empty = Vec::new();
        for entry in object
            .get("models")
            .map(|v| {
                v.as_array()
                    .ok_or_else(|| invalid("models must be an array"))
            })
            .transpose()?
            .unwrap_or(&empty)
        {
            let profile = ModelProfile::from_value(entry.clone())?;
            let aliases = entry
                .get("aliases")
                .map(|v| {
                    serde_json::from_value::<Vec<String>>(v.clone())
                        .map_err(|e| invalid(e.to_string()))
                })
                .transpose()?
                .unwrap_or_default();
            if entry.get("pricing_tiers").is_some_and(|v| !v.is_null())
                || entry.get("pricing_scope").is_some_and(|v| !v.is_null())
            {
                let base = profile
                    .pricing
                    .clone()
                    .ok_or_else(|| invalid("pricing tiers require base profile pricing"))?;
                let schedule = PricingSchedule {
                    base,
                    tiers: serde_json::from_value(
                        entry
                            .get("pricing_tiers")
                            .filter(|v| !v.is_null())
                            .cloned()
                            .unwrap_or_else(|| json!([])),
                    )
                    .map_err(|e| invalid(e.to_string()))?,
                    scope: serde_json::from_value(
                        entry
                            .get("pricing_scope")
                            .filter(|v| !v.is_null())
                            .cloned()
                            .unwrap_or_else(|| json!({})),
                    )
                    .map_err(|e| invalid(e.to_string()))?,
                };
                registry.register_with_schedule(profile, schedule, &aliases)?;
            } else {
                registry.register(profile, &aliases)?;
            }
        }
        Ok(registry)
    }
    pub fn register(&mut self, profile: ModelProfile, aliases: &[String]) -> Result<()> {
        profile.validate()?;
        let canonical = norm(&profile.model_id);
        self.schedules.remove(&canonical);
        self.aliases.insert(canonical.clone(), canonical.clone());
        if let Some((_, bare)) = canonical.split_once(':') {
            self.aliases
                .entry(bare.into())
                .or_insert_with(|| canonical.clone());
        }
        for alias in aliases {
            let alias = norm(alias);
            self.aliases.insert(alias.clone(), canonical.clone());
            if !alias.contains(':') {
                self.aliases
                    .entry(format!("{}:{alias}", profile.provider))
                    .or_insert_with(|| canonical.clone());
            }
        }
        self.profiles.insert(canonical, profile);
        Ok(())
    }
    pub fn register_with_schedule(
        &mut self,
        profile: ModelProfile,
        mut schedule: PricingSchedule,
        aliases: &[String],
    ) -> Result<()> {
        schedule.validate()?;
        if profile.pricing.as_ref() != Some(&schedule.base) {
            return Err(invalid("pricing schedule base must equal profile.pricing"));
        }
        schedule.tiers.sort_by_key(|tier| tier.min_input_tokens);
        let key = norm(&profile.model_id);
        self.register(profile, aliases)?;
        self.schedules.insert(key, schedule);
        Ok(())
    }
    pub fn get(&self, model: &str) -> Result<&ModelProfile> {
        let key = norm(model);
        let canonical = self.aliases.get(&key).or_else(|| {
            self.aliases
                .iter()
                .filter(|(alias, _)| {
                    key.strip_prefix(&format!("{alias}-")).is_some_and(|s| {
                        s.len() >= 4
                            && s.chars().any(|c| c.is_ascii_digit())
                            && s.chars()
                                .all(|c| c.is_ascii_digit() || c == '-' || c == '.')
                    })
                })
                .max_by_key(|(alias, _)| alias.len())
                .map(|(_, canonical)| canonical)
        });
        canonical.and_then(|id| self.profiles.get(id)).ok_or_else(|| Error::NotFound(format!("Unknown model {model:?}; not in the registry. Register it with ProfileRegistry::register.")))
    }
    pub fn pricing_schedule(&self, model: &str) -> Result<PricingSchedule> {
        let profile = self.get(model)?;
        if let Some(schedule) = self.schedules.get(&norm(&profile.model_id)) {
            return Ok(schedule.clone());
        }
        let base = profile
            .pricing
            .clone()
            .ok_or_else(|| invalid(format!("model {:?} has no pricing", profile.model_id)))?;
        Ok(PricingSchedule {
            base,
            tiers: Vec::new(),
            scope: PricingScope::default(),
        })
    }
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CapacityKind {
    #[default]
    Nominal,
    Effective,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LimitCheck {
    pub model_id: String,
    pub capacity_kind: CapacityKind,
    pub capacity: u64,
    pub input_tokens: u64,
    pub requested_output_tokens: Option<u64>,
    pub reserved_output_tokens: u64,
    pub context_output_tokens: u64,
    pub context_tokens: u64,
    pub max_input_tokens: u64,
    pub max_output_tokens: Option<u64>,
    pub input_exceeded: bool,
    pub context_exceeded: bool,
    pub output_exceeded: bool,
    pub allowed: bool,
    pub violations: Vec<String>,
}
pub fn check_request_limits(
    profile: &ModelProfile,
    input: u64,
    requested: Option<u64>,
    reserved: u64,
    capacity_kind: CapacityKind,
) -> Result<LimitCheck> {
    profile.validate()?;
    let capacity = match capacity_kind {
        CapacityKind::Nominal => profile.window_nominal,
        CapacityKind::Effective => profile.window_effective(),
    };
    let max_input = capacity.saturating_sub(profile.max_output.unwrap_or(0));
    let context_output = reserved.max(requested.unwrap_or(0));
    let context_tokens = input
        .checked_add(context_output)
        .ok_or_else(|| invalid("context token count overflow"))?;
    let input_exceeded = input > max_input;
    let context_exceeded = context_tokens > capacity;
    let output_exceeded = requested
        .zip(profile.max_output)
        .is_some_and(|(a, b)| a > b);
    let violations = [
        (input_exceeded, "input_tokens"),
        (context_exceeded, "context_tokens"),
        (output_exceeded, "requested_output_tokens"),
    ]
    .into_iter()
    .filter(|(flag, _)| *flag)
    .map(|(_, s)| s.into())
    .collect::<Vec<_>>();
    Ok(LimitCheck {
        model_id: profile.model_id.clone(),
        capacity_kind,
        capacity,
        input_tokens: input,
        requested_output_tokens: requested,
        reserved_output_tokens: reserved,
        context_output_tokens: context_output,
        context_tokens,
        max_input_tokens: max_input,
        max_output_tokens: profile.max_output,
        input_exceeded,
        context_exceeded,
        output_exceeded,
        allowed: violations.is_empty(),
        violations,
    })
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ExclusiveUsage {
    pub input_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
    pub output_tokens: u64,
    pub reasoning_tokens: u64,
}
impl ExclusiveUsage {
    fn counts(&self) -> [u64; 5] {
        [
            self.input_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.output_tokens,
            self.reasoning_tokens,
        ]
    }
    pub fn context_total(&self) -> Result<u64> {
        sum_counts(&self.counts())
    }
    pub fn request_input(&self) -> Result<u64> {
        sum_counts(&self.counts()[..3])
    }
}
fn sum_counts(values: &[u64]) -> Result<u64> {
    values.iter().try_fold(0u64, |a, b| {
        a.checked_add(*b)
            .ok_or_else(|| invalid("token count overflow"))
    })
}
fn usage_int(v: &Value, names: &[&str]) -> u64 {
    names
        .iter()
        .find_map(|n| v.get(*n).and_then(Value::as_u64))
        .unwrap_or(0)
}
fn has_usage_int(v: &Value, names: &[&str]) -> bool {
    names
        .iter()
        .any(|n| v.get(*n).and_then(Value::as_u64).is_some())
}
fn usage_details<'a>(v: &'a Value, names: &[&str]) -> &'a Value {
    names
        .iter()
        .find_map(|n| v.get(*n).filter(|v| v.is_object()))
        .unwrap_or(&Value::Null)
}
pub fn exclusive_usage(result: &Value) -> ExclusiveUsage {
    let Some(normalized) = result.get("usage").filter(|v| v.is_object()) else {
        return ExclusiveUsage::default();
    };
    let Some(raw) = result.get("provider_usage").filter(|v| v.is_object()) else {
        return ExclusiveUsage {
            input_tokens: usage_int(normalized, &["input_tokens", "prompt_tokens"]),
            cache_read_tokens: usage_int(
                normalized,
                &[
                    "cache_read_tokens",
                    "cached_input_tokens",
                    "cache_read_input_tokens",
                ],
            ),
            cache_write_tokens: usage_int(
                normalized,
                &[
                    "cache_write_tokens",
                    "cache_creation_input_tokens",
                    "cache_write_input_tokens",
                ],
            ),
            output_tokens: usage_int(normalized, &["output_tokens", "completion_tokens"]),
            reasoning_tokens: usage_int(normalized, &["reasoning_tokens"]),
        };
    };
    if [
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
        "cacheWriteInputTokens",
    ]
    .iter()
    .any(|key| raw.get(key).is_some())
    {
        return ExclusiveUsage {
            input_tokens: usage_int(raw, &["input_tokens", "inputTokens", "prompt_tokens"]),
            cache_read_tokens: usage_int(
                raw,
                &[
                    "cache_read_tokens",
                    "cache_read_input_tokens",
                    "cacheReadInputTokens",
                ],
            ),
            cache_write_tokens: usage_int(
                raw,
                &[
                    "cache_write_tokens",
                    "cache_creation_input_tokens",
                    "cache_write_input_tokens",
                    "cacheWriteInputTokens",
                ],
            ),
            output_tokens: usage_int(raw, &["output_tokens", "outputTokens", "completion_tokens"]),
            reasoning_tokens: usage_int(raw, &["reasoning_tokens"]),
        };
    }
    let input = if has_usage_int(raw, &["input_tokens", "prompt_tokens"]) {
        usage_int(raw, &["input_tokens", "prompt_tokens"])
    } else {
        usage_int(normalized, &["input_tokens", "prompt_tokens"])
    };
    let output = if has_usage_int(raw, &["output_tokens", "completion_tokens"]) {
        usage_int(raw, &["output_tokens", "completion_tokens"])
    } else {
        usage_int(normalized, &["output_tokens", "completion_tokens"])
    };
    let id = usage_details(raw, &["input_tokens_details", "prompt_tokens_details"]);
    let od = usage_details(raw, &["output_tokens_details", "completion_tokens_details"]);
    let fallback = |a, b| if a != 0 { a } else { b };
    let read = input.min(fallback(
        usage_int(id, &["cached_tokens"]),
        usage_int(raw, &["cached_input_tokens"]),
    ));
    let write = (input - read).min(fallback(
        usage_int(id, &["cache_write_tokens"]),
        usage_int(raw, &["cache_write_tokens", "cache_creation_input_tokens"]),
    ));
    let reasoning = output.min(fallback(
        usage_int(od, &["reasoning_tokens"]),
        usage_int(raw, &["reasoning_tokens"]),
    ));
    ExclusiveUsage {
        input_tokens: input - read - write,
        cache_read_tokens: read,
        cache_write_tokens: write,
        output_tokens: output - reasoning,
        reasoning_tokens: reasoning,
    }
}

/// The wire quote follows Tokenmaster, while total is computed with Decimal.
#[derive(Debug, Clone)]
pub struct DecimalQuote {
    pub total: Decimal,
    pub metadata: Value,
}
fn decimal_cost(counts: &[u64], rates: &[f64]) -> Result<Decimal> {
    let mut preferred_scale = 0;
    let terms = counts
        .iter()
        .zip(rates)
        .map(|(count, rate)| {
            // Python constructs Decimal from str(float): retain the preferred
            // decimal scale, including trailing .0 and zero-valued quotations.
            let text = crate::result_text_and_digest(&json!(rate))?.0;
            let price = crate::parse_decimal_exact(&text).map_err(|e| invalid(e.to_string()))?;
            preferred_scale = preferred_scale.max(price.scale());
            Ok((*count, price))
        })
        .collect::<Result<Vec<_>>>()?;
    let mut quotient = crate::decimal::cost_per_million(&terms)
        .ok_or_else(|| invalid("cost cannot be represented exactly as a native decimal"))?;
    quotient.rescale(quotient.scale().max(preferred_scale));
    Ok(quotient)
}
pub fn quote_usage(
    registry: &ProfileRegistry,
    model: &str,
    usage: &ExclusiveUsage,
) -> Result<DecimalQuote> {
    let profile = registry.get(model)?;
    let schedule = registry.pricing_schedule(model)?;
    for (category, count) in CATEGORIES.iter().zip(usage.counts()) {
        if count > 0
            && schedule
                .scope
                .unpriced_usage_categories
                .iter()
                .any(|s| s == category)
        {
            return Err(invalid(format!(
                "model {:?} pricing does not cover usage categories: {category}",
                profile.model_id
            )));
        }
    }
    let input = usage.request_input()?;
    let (price, tier) = schedule.price_for(input)?;
    let rates = [
        price.input,
        price.cache_read,
        price.cache_write,
        price.output,
        price.output,
    ];
    let costs = usage
        .counts()
        .iter()
        .zip(rates)
        .map(|(n, r)| *n as f64 * r / 1_000_000.0)
        .collect::<Vec<_>>();
    let total = decimal_cost(&usage.counts(), &rates)?;
    Ok(DecimalQuote {
        total,
        metadata: json!({"model_id":profile.model_id,"tier_basis_tokens":input,"tier_min_input_tokens":tier,"pricing":price,"input_cost":costs[0],"cache_read_cost":costs[1],"cache_write_cost":costs[2],"output_cost":costs[3],"reasoning_cost":costs[4],"total_cost":costs.iter().sum::<f64>(),"currency":price.currency,"as_of":price.as_of,"source":profile.source}),
    })
}
pub fn quote_estimate(
    registry: &ProfileRegistry,
    model: &str,
    input: u64,
    reserved: u64,
    conservative: bool,
) -> Result<DecimalQuote> {
    let profile = registry.get(model)?;
    let schedule = registry.pricing_schedule(model)?;
    let mut unpriced_input = schedule
        .scope
        .unpriced_usage_categories
        .iter()
        .filter(|s| CATEGORIES[..3].contains(&s.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    unpriced_input.sort();
    let mut unpriced_output = schedule
        .scope
        .unpriced_usage_categories
        .iter()
        .filter(|s| CATEGORIES[3..].contains(&s.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    unpriced_output.sort();
    if input > 0
        && (unpriced_input.iter().any(|s| s == "input_tokens")
            || (conservative && !unpriced_input.is_empty()))
    {
        return Err(invalid(format!("model {:?} pricing cannot conservatively bound estimated input because these categories are unpriced: {}",profile.model_id,unpriced_input.join(", "))));
    }
    if reserved > 0 && !unpriced_output.is_empty() {
        return Err(invalid(format!("model {:?} pricing cannot bound reserved output because these categories are unpriced: {}",profile.model_id,unpriced_output.join(", "))));
    }
    let (price, tier) = schedule.price_for(input)?;
    let mut selected = ("input", price.input);
    if conservative {
        for entry in [
            ("cache_read", price.cache_read),
            ("cache_write", price.cache_write),
        ] {
            if entry.1 > selected.1 {
                selected = entry;
            }
        }
    }
    let mut assumptions = vec![
        if conservative {
            "estimated input uses the highest selected-tier input-category rate"
        } else {
            "estimated input is treated as uncached input"
        },
        "reserved output uses the selected-tier output rate",
    ];
    if !conservative && !unpriced_input.is_empty() {
        assumptions.push("unpriced cache-write storage is excluded; valid only when no explicit cache is created");
    }
    let input_cost = input as f64 * selected.1 / 1_000_000.0;
    let output_cost = reserved as f64 * price.output / 1_000_000.0;
    Ok(DecimalQuote {
        total: decimal_cost(&[input, reserved], &[selected.1, price.output])?,
        metadata: json!({"model_id":profile.model_id,"tier_basis_tokens":input,"tier_min_input_tokens":tier,"pricing":price,"input_tokens":input,"reserved_output_tokens":reserved,"input_rate_kind":selected.0,"input_rate":selected.1,"output_rate":price.output,"input_cost":input_cost,"output_cost":output_cost,"total_cost":input_cost+output_cost,"currency":price.currency,"as_of":price.as_of,"source":profile.source,"conservative":conservative,"assumptions":assumptions}),
    })
}

fn requested_output(payload: &Value) -> Result<Option<u64>> {
    for key in ["max_output_tokens", "max_completion_tokens", "max_tokens"] {
        if let Some(v) = payload.get(key) {
            return v
                .as_u64()
                .map(Some)
                .ok_or_else(|| invalid(format!("{key} must be a non-negative int")));
        }
    }
    Ok(None)
}
fn model_string(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}
fn target(bound: &Option<String>, payload: &Value, result: Option<&Value>) -> Option<String> {
    bound
        .clone()
        .or_else(|| {
            result.and_then(|r| {
                model_string(r.get("model"))
                    .or_else(|| model_string(r.get("usage").and_then(|v| v.get("model_id"))))
            })
        })
        .or_else(|| model_string(payload.get("model")))
}
fn tm_meta(meta: &mut Value) -> &mut Value {
    if !meta.is_object() {
        *meta = json!({});
    }
    if !meta.get("tokenmaster").is_some_and(Value::is_object) {
        meta["tokenmaster"] = json!({});
    }
    &mut meta["tokenmaster"]
}
fn unavailable(
    meter: &str,
    model: Option<&str>,
    field: &str,
    reason: &str,
    code: &str,
    message: &str,
    error: Option<&Error>,
) -> Error {
    let mut diagnostics = json!({"status":"unavailable","reason":code});
    if let Some(error) = error {
        diagnostics["error"] = json!(detail(error));
    }
    let mut audit = json!({"meter":meter,"tokenmaster":{}});
    if let Some(model) = model {
        audit["tokenmaster"]["model_id"] = json!(model);
    }
    audit["tokenmaster"][field] = diagnostics;
    MeterPrecheckRefusal {
        reason: reason.into(),
        detail: message.into(),
        audit_meta: audit,
        requested: None,
        remaining: None,
    }
    .into()
}

/// Native advisor callback. It receives the exact state and optional task wire objects.
pub type TokenmasterAdvisor = Rc<dyn Fn(&Value, Option<&Value>) -> Result<Value>>;
#[derive(Default)]
struct Gauge {
    turns: u64,
    used: u64,
    mean: Option<f64>,
    variance: f64,
}
pub struct TokenmasterMeter {
    registry: Rc<ProfileRegistry>,
    model: Option<String>,
    estimator: Option<TokenEstimator>,
    reserved_output: u64,
    enforce: bool,
    capacity: CapacityKind,
    task: Option<Value>,
    advisor: Option<TokenmasterAdvisor>,
    gauges: RefCell<BTreeMap<String, Gauge>>,
}
impl TokenmasterMeter {
    pub fn new(registry: Rc<ProfileRegistry>) -> Self {
        Self {
            registry,
            model: None,
            estimator: None,
            reserved_output: 0,
            enforce: false,
            capacity: CapacityKind::Nominal,
            task: None,
            advisor: None,
            gauges: RefCell::new(BTreeMap::new()),
        }
    }
    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = Some(model.into());
        self
    }
    pub fn with_estimator(mut self, estimator: TokenEstimator) -> Self {
        self.estimator = Some(estimator);
        self
    }
    pub fn with_reserved_output(mut self, tokens: u64) -> Self {
        self.reserved_output = tokens;
        self
    }
    pub fn with_profile_limits(mut self, capacity: CapacityKind) -> Result<Self> {
        if self.estimator.is_none() {
            return Err(invalid("profile-limit enforcement requires an estimator"));
        }
        self.enforce = true;
        self.capacity = capacity;
        Ok(self)
    }
    pub fn with_task(mut self, task: Value) -> Result<Self> {
        if !task.is_object() {
            return Err(invalid("task must be an object"));
        }
        self.task = Some(task);
        Ok(self)
    }
    pub fn with_expected_remaining_turns(mut self, turns: u64) -> Self {
        self.task = Some(json!({"expected_remaining_turns":turns,"task_criticality":"normal"}));
        self
    }
    pub fn with_advisor(mut self, advisor: TokenmasterAdvisor) -> Self {
        self.advisor = Some(advisor);
        self
    }
    fn limits(&self, model: Option<&str>, usage: &ExclusiveUsage) -> Value {
        let Some(model) = model else {
            return json!({"status":"unavailable","reason":"missing_model"});
        };
        let check = self.registry.get(model).and_then(|p| {
            check_request_limits(
                p,
                usage.request_input()?,
                Some(sum_counts(&[usage.output_tokens, usage.reasoning_tokens])?),
                self.reserved_output,
                self.capacity,
            )
        });
        match check {
            Ok(check) => {
                let mut v = wire(&check);
                v["phase"] = json!("settlement");
                v
            }
            Err(e) => {
                json!({"status":"unavailable","reason":"profile_lookup_failed","error":detail(&e)})
            }
        }
    }
    fn settle(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        if kind != NodeKind::ModelCall || !result.get("usage").is_some_and(Value::is_object) {
            return Ok(0.0);
        }
        let usage = exclusive_usage(result);
        let charge = usage.context_total()?;
        let model = target(&self.model, payload, Some(result));
        if self.enforce {
            tm_meta(meta)["limits"] = self.limits(model.as_deref(), &usage);
        }
        let Some(model) = model else {
            return Ok(charge as f64);
        };
        let settlement = (|| {
            let profile = self.registry.get(&model)?;
            let mut gauges = self.gauges.borrow_mut();
            let gauge = gauges.entry(profile.model_id.clone()).or_default();
            if gauge.turns > 0 {
                let growth = charge as f64 - gauge.used as f64;
                if let Some(mean) = gauge.mean {
                    let diff = growth - mean;
                    let incr = 0.3 * diff;
                    gauge.mean = Some(mean + incr);
                    gauge.variance = 0.7 * (gauge.variance + diff * incr);
                } else {
                    gauge.mean = Some(growth);
                }
            }
            gauge.turns = gauge
                .turns
                .checked_add(1)
                .ok_or_else(|| invalid("turn count overflow"))?;
            gauge.used = charge;
            let mut turn = wire(&usage);
            for(k,v)in json!({"turn_id":gauge.turns,"model_id":profile.model_id,"timestamp":timestamp(),"breakdown":null,"source":"reported","raw":null,"schema_version":"0.1"}).as_object().unwrap(){turn[k]=v.clone();}
            let state = gauge_state(profile, gauge, &usage, self.reserved_output);
            let advice = match &self.advisor {
                Some(a) => a(&state, self.task.as_ref())?,
                None => threshold_advice(&state, self.task.as_ref()),
            };
            Ok::<_, Error>((turn, state, advice))
        })();
        match settlement {
            Ok((turn, state, advice)) => {
                let tm = tm_meta(meta);
                tm["turn"] = turn;
                tm["state"] = state;
                tm["advice"] = advice;
                if let Some(task) = &self.task {
                    tm["task"] = task.clone();
                }
            }
            Err(e) => tm_meta(meta)["meter"] = json!({"status":"error","error":detail(&e)}),
        }
        Ok(charge as f64)
    }
}
impl Meter for TokenmasterMeter {
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
        let Some(estimator) = &self.estimator else {
            return Ok(None);
        };
        let model = target(&self.model, payload, None);
        let estimate = estimator(payload)?;
        let missing = |code, message| {
            unavailable(
                "tokens",
                model.as_deref(),
                "limits",
                "tokenmaster_profile_unavailable",
                code,
                message,
                None,
            )
        };
        let Some(estimate) = estimate else {
            if self.enforce {
                return Err(missing(
                    "missing_input_estimate",
                    "tokenmaster profile enforcement needs an input-token estimate",
                ));
            }
            return Ok(None);
        };
        if !self.enforce {
            return Ok(Some(sum_counts(&[estimate, self.reserved_output])? as f64));
        }
        let requested = requested_output(payload)?;
        let model = model.as_deref().ok_or_else(|| {
            missing(
                "missing_model",
                "tokenmaster profile enforcement needs a model id",
            )
        })?;
        let check = self
            .registry
            .get(model)
            .and_then(|p| {
                check_request_limits(p, estimate, requested, self.reserved_output, self.capacity)
            })
            .map_err(|e| {
                unavailable(
                    "tokens",
                    Some(model),
                    "limits",
                    "tokenmaster_profile_unavailable",
                    "profile_lookup_failed",
                    &format!("tokenmaster could not resolve request limits for {model}"),
                    Some(&e),
                )
            })?;
        if !check.allowed {
            let (requested, remaining) = if check.input_exceeded {
                (check.input_tokens, check.max_input_tokens)
            } else if check.context_exceeded {
                (check.context_tokens, check.capacity)
            } else {
                (
                    check.requested_output_tokens.unwrap_or(0),
                    check.max_output_tokens.unwrap_or(0),
                )
            };
            return Err(MeterPrecheckRefusal{reason:"tokenmaster_profile_limit".into(),detail:format!("tokenmaster model profile refused the request: {}",check.violations.join(", ")),audit_meta:json!({"meter":"tokens","tokenmaster":{"model_id":check.model_id,"limits":check}}),requested:Some(requested.to_string()),remaining:Some(remaining.to_string())}.into());
        }
        Ok(Some(check.context_tokens as f64))
    }
    fn charge(&self, kind: NodeKind, payload: &Value, result: &Value, meta: &Value) -> Result<f64> {
        self.settle(kind, payload, result, &mut meta.clone())
    }
    fn charge_with_meta(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        self.settle(kind, payload, result, meta)
    }
}

pub struct TokenmasterCostMeter {
    registry: Rc<ProfileRegistry>,
    model: Option<String>,
    estimator: TokenEstimator,
    reserved_output: u64,
    name: String,
}
impl TokenmasterCostMeter {
    pub fn new(registry: Rc<ProfileRegistry>, estimator: TokenEstimator) -> Self {
        Self {
            registry,
            model: None,
            estimator,
            reserved_output: 0,
            name: "usd".into(),
        }
    }
    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = Some(model.into());
        self
    }
    pub fn with_reserved_output(mut self, tokens: u64) -> Self {
        self.reserved_output = tokens;
        self
    }
    pub fn with_name(mut self, name: impl Into<String>) -> Result<Self> {
        let name = name.into();
        if name.is_empty() {
            return Err(invalid("cost meter name must be a non-empty string"));
        }
        self.name = name;
        Ok(self)
    }
    fn settle(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        if kind != NodeKind::ModelCall || !result.get("usage").is_some_and(Value::is_object) {
            return Ok(0.0);
        }
        let Some(model) = target(&self.model, payload, Some(result)) else {
            tm_meta(meta)["cost"] = json!({"status":"unavailable","reason":"missing_model"});
            return Ok(0.0);
        };
        let quote = quote_usage(&self.registry, &model, &exclusive_usage(result)).and_then(|q| {
            if q.metadata["currency"] != json!("USD") {
                Err(invalid(format!(
                    "unsupported pricing currency {:?}",
                    q.metadata["currency"].as_str().unwrap_or_default()
                )))
            } else {
                Ok(q)
            }
        });
        match quote {
            Ok(mut q) => {
                q.metadata["status"] = json!("quoted");
                q.metadata["total_cost_decimal"] = json!(q.total.to_string());
                let amount = q
                    .total
                    .to_f64()
                    .ok_or_else(|| invalid("cost exceeds float range"))?;
                tm_meta(meta)["cost"] = q.metadata;
                Ok(amount)
            }
            Err(e) => {
                tm_meta(meta)["cost"] =
                    json!({"status":"unavailable","reason":"pricing_failed","error":detail(&e)});
                Ok(0.0)
            }
        }
    }
}
impl Meter for TokenmasterCostMeter {
    fn name(&self) -> &str {
        &self.name
    }
    fn precheck_is_estimate(&self) -> bool {
        true
    }
    fn estimate(&self, kind: NodeKind, payload: &Value) -> Result<Option<f64>> {
        if kind != NodeKind::ModelCall {
            return Ok(None);
        }
        let model = target(&self.model, payload, None);
        let refuse = |code, message, error: Option<&Error>| {
            unavailable(
                &self.name,
                model.as_deref(),
                "pricing",
                "tokenmaster_pricing_unavailable",
                code,
                message,
                error,
            )
        };
        let input = (self.estimator)(payload)?.ok_or_else(|| {
            refuse(
                "missing_input_estimate",
                "tokenmaster USD governance needs an input-token estimate",
                None,
            )
        })?;
        let model = model.as_deref().ok_or_else(|| {
            refuse(
                "missing_model",
                "tokenmaster USD governance needs a model id",
                None,
            )
        })?;
        let reserve = self
            .reserved_output
            .max(requested_output(payload)?.unwrap_or(0));
        let quote = quote_estimate(&self.registry, model, input, reserve, true).map_err(|e| {
            refuse(
                "pricing_lookup_failed",
                "tokenmaster could not conservatively price the request",
                Some(&e),
            )
        })?;
        if quote.metadata["currency"] != json!("USD") {
            return Err(MeterPrecheckRefusal{reason:"tokenmaster_currency".into(),detail:format!("tokenmaster USD governance does not support {} pricing",quote.metadata["currency"].as_str().unwrap_or_default()),audit_meta:json!({"meter":self.name,"tokenmaster":{"model_id":quote.metadata["model_id"],"pricing":{"status":"unsupported_currency","currency":quote.metadata["currency"]}}}),requested:None,remaining:None}.into());
        }
        Ok(Some(
            quote
                .total
                .to_f64()
                .ok_or_else(|| invalid("cost exceeds float range"))?,
        ))
    }
    fn charge(&self, kind: NodeKind, payload: &Value, result: &Value, meta: &Value) -> Result<f64> {
        self.settle(kind, payload, result, &mut meta.clone())
    }
    fn charge_with_meta(
        &self,
        kind: NodeKind,
        payload: &Value,
        result: &Value,
        meta: &mut Value,
    ) -> Result<f64> {
        self.settle(kind, payload, result, meta)
    }
    fn precheck_fallback_reason(
        &self,
        kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        meta: &Value,
    ) -> Option<String> {
        (kind == NodeKind::ModelCall
            && meta["tokenmaster"]["cost"]["status"] == json!("unavailable"))
        .then(|| "exact_pricing_unavailable".into())
    }
}

fn gauge_state(
    profile: &ModelProfile,
    gauge: &Gauge,
    usage: &ExclusiveUsage,
    reserved: u64,
) -> Value {
    let effective = profile.window_effective();
    let hn = i128::from(profile.window_nominal) - i128::from(gauge.used) - i128::from(reserved);
    let he = i128::from(effective) - i128::from(gauge.used) - i128::from(reserved);
    let fill = gauge.used as f64 / effective as f64;
    let source = profile.effective_source();
    let mut provenance = json!({"window_effective":source,"used_tokens":"reported"});
    let mut velocity = None;
    let mut std = None;
    let mut eta = Value::Null;
    if gauge.turns >= 3 {
        if let Some(mean) = gauge.mean {
            velocity = Some(mean);
            std = Some(gauge.variance.sqrt());
            provenance["velocity"] = json!("derived (ewma alpha=0.3)");
            if he <= 0 {
                provenance["eta_turns"] = json!("exhausted (no headroom remaining)");
            } else if mean > 0.0 {
                eta = json!({"expected":he as f64/mean,"conservative":he as f64/(mean+gauge.variance.sqrt())});
                provenance["eta_turns"] = json!("derived");
            } else {
                provenance["eta_turns"] = json!("unavailable (velocity not positive)");
            }
        }
    } else {
        provenance["velocity"] = json!("unavailable (cold start, needs 3 turns)");
        provenance["eta_turns"] = provenance["velocity"].clone();
    }
    let cache = if usage.cache_read_tokens > 0 || usage.cache_write_tokens > 0 {
        provenance["cache"] = json!("estimated");
        json!({"stable_prefix_tokens":u128::from(usage.cache_read_tokens)+u128::from(usage.cache_write_tokens),"last_cache_read":usage.cache_read_tokens,"last_cache_write":usage.cache_write_tokens})
    } else {
        Value::Null
    };
    json!({"model_id":profile.model_id,"turns":gauge.turns,"used_tokens":gauge.used,"window_nominal":profile.window_nominal,"window_effective":effective,"effective_source":source,"reserved_output":reserved,"headroom_nominal":hn,"headroom_effective":he,"fill_nominal":gauge.used as f64/profile.window_nominal as f64,"fill_effective":fill,"velocity":velocity,"velocity_std":std,"eta_turns":eta,"zone":if fill>=0.85{"critical"}else if fill>=0.7{"caution"}else{"green"},"hidden_overhead":null,"cache":cache,"provenance":provenance,"schema_version":"0.1"})
}
pub fn threshold_advice(state: &Value, task: Option<&Value>) -> Value {
    let fill = state["fill_effective"].as_f64().unwrap_or(0.0);
    let head = state["headroom_effective"]
        .as_i64()
        .map(i128::from)
        .or_else(|| {
            state["headroom_effective"]
                .as_number()
                .and_then(|n| n.to_string().parse::<i128>().ok())
        })
        .unwrap_or(0);
    let (action, urgency, comparison) = if head <= 0 {
        (
            "compact",
            "now",
            format!("headroom_effective {head} <= 0 (exhausted)"),
        )
    } else if fill >= 0.85 {
        (
            "compact",
            "now",
            format!("fill {fill:.3} >= compact_at 0.85"),
        )
    } else if fill >= 0.7 {
        (
            "compact",
            "soon",
            format!("warn_at 0.70 <= fill {fill:.3} < compact_at 0.85"),
        )
    } else {
        ("continue", "none", format!("fill {fill:.3} < warn_at 0.70"))
    };
    json!({"action":action,"urgency":urgency,"rationale":{"inputs":{"fill_effective":fill,"headroom_effective":head,"warn_at":0.7,"compact_at":0.85,"expected_remaining_turns":task.and_then(|t|t.get("expected_remaining_turns"))},"derived":{"note":"threshold baseline estimates no effects"},"comparison":comparison},"expected":{"tokens_spent":null,"tokens_freed":null,"cost_delta":null,"fidelity_risk":null},"policy_id":"threshold","schema_version":"0.1"})
}

fn timestamp() -> String {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let seconds = elapsed.as_secs();
    let z = (seconds / 86400) as i64 + 719468;
    let era = z / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:06}+00:00",
        seconds % 86400 / 3600,
        seconds % 3600 / 60,
        seconds % 60,
        elapsed.subsec_micros()
    )
}

/// Explicit standard runtime meter set with native Tokenmaster governance.
pub fn tokenmaster_governance_meters(
    tokens: TokenmasterMeter,
    cost: TokenmasterCostMeter,
) -> Vec<Rc<dyn Meter>> {
    vec![
        Rc::new(crate::StepMeter),
        Rc::new(crate::DepthMeter),
        Rc::new(crate::WallClockMeter),
        Rc::new(tokens),
        Rc::new(cost),
    ]
}
