//! Shared serializable key/value storage and exact reservation ledger.
//! Wire buckets and permanent retry tombstones match Python Pollard 1.6.0.
use crate::{
    canonical_bytes, json, BudgetReservation, Error, LeaseRenewer, Node, NodeKind, RecordingStore,
    ReservationCheck, Result, Store, Value, WindowReservation,
};
use rust_decimal::Decimal;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

pub trait KvTransaction {
    fn get(&mut self, bucket: &str, key: &str) -> Result<Option<String>>;
    fn items(&mut self, bucket: &str) -> Result<Vec<(String, String)>>;
    fn put(&mut self, bucket: &str, key: &str, value: &str) -> Result<()>;
    fn delete(&mut self, bucket: &str, key: &str) -> Result<()>;
    /// Validated server time captured after the backend's transaction lock.
    fn now(&self) -> f64;
}
/// Callbacks may be rerun on transaction conflicts; they must have no external effects.
/// Drivers validate namespace identity and provide one serializable snapshot.
pub trait KvBackend: Send + Sync + 'static {
    fn transact(
        &self,
        writable: bool,
        callback: &mut dyn FnMut(&mut dyn KvTransaction) -> Result<()>,
    ) -> Result<()>;
    fn reconnect(&self) -> Result<()>;
}
pub struct TransactionalKvStore<B: KvBackend> {
    backend: Arc<B>,
    read_only: bool,
}
impl<B: KvBackend> TransactionalKvStore<B> {
    /// The driver must atomically initialize its coordinator and schema first.
    /// Existing/missing namespaces are validated here, never repaired.
    pub fn from_backend(backend: B, read_only: bool) -> Result<Self> {
        let store = Self {
            backend: Arc::new(backend),
            read_only,
        };
        store.once(false, |tx| {
            if tx.get("schema", "version")?.as_deref() != Some("1") {
                return Err(integrity(
                    "unsupported or missing transactional schema version",
                ));
            }
            Ok(())
        })?;
        Ok(store)
    }
    pub fn backend(&self) -> &B {
        &self.backend
    }
    pub fn reconnect(&self) -> Result<()> {
        self.backend.reconnect()
    }
    fn once<T>(
        &self,
        write: bool,
        mut callback: impl FnMut(&mut dyn KvTransaction) -> Result<T>,
    ) -> Result<T> {
        if write && self.read_only {
            return Err(Error::Invalid("store is read-only".into()));
        }
        let mut result = None;
        self.backend.transact(write, &mut |tx| {
            let now = tx.now();
            if !now.is_finite() || now < 0.0 {
                return Err(integrity("invalid backend server clock"));
            }
            result = Some(callback(tx)?);
            Ok(())
        })?;
        result.ok_or_else(|| integrity("backend did not execute transaction callback"))
    }
    fn run<T>(
        &self,
        write: bool,
        mut callback: impl FnMut(&mut dyn KvTransaction) -> Result<T>,
    ) -> Result<T> {
        match self.once(write, &mut callback) {
            Err(e) if connection_lost(&e) => {
                self.backend.reconnect()?;
                self.once(write, callback)
            }
            result => result,
        }
    }
    fn arbiter<T>(
        &self,
        id: &str,
        settlement: bool,
        mut callback: impl FnMut(&mut dyn KvTransaction) -> Result<T>,
    ) -> Result<T> {
        match self.once(true, &mut callback) {
            Err(e) if connection_lost(&e) => {
                let retry = self
                    .backend
                    .reconnect()
                    .and_then(|_| self.once(true, callback));
                match retry {
                    Err(e) if connection_lost(&e) => Err(if settlement {
                        Error::SettlementUncertain {
                            reservation_id: id.into(),
                        }
                    } else {
                        Error::ReservationUncertain {
                            reservation_id: id.into(),
                        }
                    }),
                    result => result,
                }
            }
            result => result,
        }
    }
    pub fn reserve(
        &self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease: f64,
    ) -> Result<ReservationCheck> {
        valid_seconds(lease)?;
        self.arbiter(id, false, |tx| {
            reserve_once(tx, id, budgets, windows, lease)
        })
    }
    pub fn settle(&self, id: &str, charges: &BTreeMap<String, Decimal>) -> Result<()> {
        self.arbiter(id, true, |tx| settle_once(tx, id, charges))
    }
    /// Retain Python Decimal spelling in request fingerprints and ledger details.
    pub fn reserve_decimal_text(
        &self,
        id: &str,
        budgets: &[crate::TextBudgetReservation],
        windows: &[crate::TextWindowReservation],
        lease: f64,
    ) -> Result<ReservationCheck> {
        let prepared = crate::decimal_wire::prepare_request(budgets, windows, lease)?;
        self.arbiter(id, false, |tx| {
            reserve_encoded_once(
                tx,
                id,
                &prepared.budgets,
                &prepared.windows,
                lease,
                &prepared.encoded,
                Some(&prepared.document),
            )
        })
    }
    /// Retain Python Decimal spelling when creating or retrying settlement.
    pub fn settle_decimal_text(&self, id: &str, charges: &BTreeMap<String, String>) -> Result<()> {
        let prepared = crate::decimal_wire::prepare_charges(charges)?;
        self.arbiter(id, true, |tx| {
            settle_encoded_once(
                tx,
                id,
                &prepared.amounts,
                &prepared.encoded,
                Some(&prepared.document),
            )
        })
    }
    pub fn release(&self, id: &str) -> Result<()> {
        self.arbiter(id, false, |tx| release_once(tx, id))
    }
    pub fn renew(&self, id: &str, lease: f64) -> Result<bool> {
        valid_seconds(lease)?;
        self.run(true, |tx| renew_once(tx, id, lease))
    }
}
fn integrity(message: impl Into<String>) -> Error {
    Error::Integrity(message.into())
}
fn connection_lost(error: &Error) -> bool {
    matches!(
        error,
        Error::Backend {
            connection_lost: true,
            ..
        }
    )
}
fn valid_seconds(seconds: f64) -> Result<()> {
    if !seconds.is_finite() || seconds <= 0.0 {
        Err(Error::Invalid(
            "lease/window seconds must be finite and positive".into(),
        ))
    } else {
        Ok(())
    }
}
fn text(value: &Value) -> Result<String> {
    Ok(crate::result_text_and_digest(value)?.0)
}
fn object(text: &str) -> Result<Value> {
    let value: Value = serde_json::from_str(text).map_err(|e| integrity(e.to_string()))?;
    if !value.is_object() {
        return Err(integrity("stored document must be an object"));
    }
    Ok(value)
}
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| integrity(format!("stored {key} must be a string")))
}
fn float(value: &Value, key: &str) -> Result<f64> {
    value
        .get(key)
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite())
        .ok_or_else(|| integrity(format!("stored {key} must be a finite number")))
}
fn dec(value: &str) -> Result<Decimal> {
    let amount = crate::parse_decimal_exact(value)
        .map_err(|e| integrity(format!("invalid decimal ledger value: {e}")))?;
    if amount < Decimal::ZERO {
        return Err(integrity("stored ledger amounts must be nonnegative"));
    }
    Ok(amount)
}
fn plus(a: Decimal, b: Decimal) -> Result<Decimal> {
    crate::decimal::exact_add(a, b)
        .ok_or_else(|| integrity("decimal ledger result is not exactly representable"))
}
fn minus(a: Decimal, b: Decimal) -> Result<Decimal> {
    crate::decimal::exact_subtract(a, b)
        .ok_or_else(|| integrity("decimal ledger result is not exactly representable"))
}
pub fn decimal_string(value: Decimal) -> String {
    crate::meters::python_decimal_string(&value.to_string()).expect("Decimal has valid finite text")
}
fn decimal_map(values: &BTreeMap<String, Decimal>) -> Value {
    json!(values
        .iter()
        .map(|(k, v)| (k, decimal_string(*v)))
        .collect::<BTreeMap<_, _>>())
}
fn float_string(value: f64) -> Result<String> {
    text(&json!(value))
}
pub fn compound_key(parts: &[&str]) -> Result<String> {
    String::from_utf8(canonical_bytes(&json!(parts))?).map_err(|e| integrity(e.to_string()))
}
pub fn reservation_request(
    budgets: &[BudgetReservation],
    windows: &[WindowReservation],
    lease: f64,
) -> Result<(String, String)> {
    valid_seconds(lease)?;
    let mut budgets = budgets.iter().collect::<Vec<_>>();
    budgets.sort_by(|a, b| a.scope_id.cmp(&b.scope_id));
    let mut windows = windows.iter().collect::<Vec<_>>();
    windows.sort_by(|a, b| a.ledger_key.cmp(&b.ledger_key));
    for b in &budgets {
        for amount in b
            .limits
            .values()
            .chain(b.baseline.values())
            .chain(b.estimates.values())
        {
            if *amount < Decimal::ZERO {
                return Err(Error::Invalid(
                    "reservation amounts must be nonnegative".into(),
                ));
            }
        }
    }
    let mut window_values = Vec::new();
    for w in windows {
        valid_seconds(w.window_seconds)?;
        if w.limit < Decimal::ZERO || w.amount < Decimal::ZERO {
            return Err(Error::Invalid(
                "reservation amounts must be nonnegative".into(),
            ));
        }
        window_values.push(json!({"ledger_key":w.ledger_key,"meter":w.meter,"limit":decimal_string(w.limit),"amount":decimal_string(w.amount),"window_seconds":float_string(w.window_seconds)?}));
    }
    let value = json!({"budgets":budgets.iter().map(|b|json!({"scope_id":b.scope_id,"limits":decimal_map(&b.limits),"baseline":decimal_map(&b.baseline),"estimates":decimal_map(&b.estimates)})).collect::<Vec<_>>(),"windows":window_values,"lease_seconds":float_string(lease)?});
    digest_document(&value)
}
pub fn reservation_charges(charges: &BTreeMap<String, Decimal>) -> Result<(String, String)> {
    if charges.values().any(|v| *v < Decimal::ZERO) {
        return Err(Error::Invalid("charges must be nonnegative".into()));
    }
    digest_document(&decimal_map(charges))
}
pub(crate) fn digest_document(value: &Value) -> Result<(String, String)> {
    let bytes = canonical_bytes(value)?;
    let digest = crate::identity::hash(&[], &bytes);
    Ok((
        String::from_utf8(bytes).map_err(|e| integrity(e.to_string()))?,
        digest,
    ))
}
pub fn node_text(node: &Node) -> Result<String> {
    text(
        &json!({"id":node.id,"parent":node.parent,"kind":node.kind,"attempt":node.attempt,"payload":String::from_utf8(canonical_bytes(&node.payload)?).map_err(|e|integrity(e.to_string()))?,"result":node.result_text,"result_digest":node.result_digest,"meta":text(&node.meta)?}),
    )
}
pub fn node_from_text(raw: &str) -> Result<Node> {
    let v = object(raw)?;
    let optional = |key| -> Result<Option<String>> {
        match v.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            _ => Err(integrity(format!("stored {key} must be string or null"))),
        }
    };
    let kind: NodeKind =
        serde_json::from_value(v["kind"].clone()).map_err(|e| integrity(e.to_string()))?;
    Node::from_storage(
        string(&v, "id")?.into(),
        optional("parent")?,
        kind,
        v["attempt"]
            .as_u64()
            .ok_or_else(|| integrity("stored attempt must be nonnegative integer"))?,
        string(&v, "payload")?,
        optional("result")?,
        optional("result_digest")?,
        string(&v, "meta")?,
    )
    .map_err(|e| integrity(e.to_string()))
}
fn same_identity(a: &Node, b: &Node) -> bool {
    a.parent == b.parent && a.kind == b.kind && a.attempt == b.attempt && a.payload == b.payload
}
fn put_node(tx: &mut dyn KvTransaction, node: &Node) -> Result<()> {
    node.validate()?;
    if let Some(parent) = &node.parent {
        if tx.get("nodes", parent)?.is_none() {
            return Err(Error::NotFound(parent.clone()));
        }
    }
    if let Some(current) = tx.get("nodes", &node.id)? {
        let mut existing = node_from_text(&current)?;
        if !same_identity(&existing, node) {
            return Err(integrity("node identity collision"));
        }
        if node.result_text.is_none() || node.result_text == existing.result_text {
            return Ok(());
        }
        let conflict = json!({"result_digest":node.result_digest,"result":node.result});
        let conflicts = existing
            .meta
            .as_object_mut()
            .ok_or_else(|| integrity("invalid metadata"))?
            .entry("result_conflicts")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| integrity("invalid conflict metadata"))?;
        if conflicts
            .iter()
            .any(|v| crate::identity::result_values_equal(v, &conflict))
        {
            return Ok(());
        }
        conflicts.push(conflict);
        tx.put("nodes", &node.id, &node_text(&existing)?)
    } else {
        tx.put("nodes", &node.id, &node_text(node)?)
    }
}
fn patch_node(tx: &mut dyn KvTransaction, id: &str, patch: &Value) -> Result<()> {
    let patch = patch
        .as_object()
        .ok_or_else(|| Error::Invalid("metadata patch must be an object".into()))?;
    let mut node = node_from_text(
        &tx.get("nodes", id)?
            .ok_or_else(|| Error::NotFound(id.into()))?,
    )?;
    node.meta
        .as_object_mut()
        .ok_or_else(|| integrity("invalid metadata"))?
        .extend(patch.clone());
    crate::identity::validate_finite_json(&node.meta)?;
    tx.put("nodes", id, &node_text(&node)?)
}
fn nodes(tx: &mut dyn KvTransaction) -> Result<Vec<Node>> {
    tx.items("nodes")?
        .into_iter()
        .map(|(_, raw)| node_from_text(&raw))
        .collect()
}
impl<B: KvBackend> Store for TransactionalKvStore<B> {
    fn put(&mut self, node: Node) -> Result<()> {
        self.run(true, |tx| put_node(tx, &node))
    }
    fn get(&self, id: &str) -> Result<Node> {
        self.run(false, |tx| {
            node_from_text(
                &tx.get("nodes", id)?
                    .ok_or_else(|| Error::NotFound(id.into()))?,
            )
        })
    }
    fn exists(&self, id: &str) -> bool {
        self.try_exists(id).unwrap_or(true)
    }
    fn try_exists(&self, id: &str) -> Result<bool> {
        self.run(false, |tx| Ok(tx.get("nodes", id)?.is_some()))
    }
    fn children(&self, id: &str) -> Result<Vec<String>> {
        self.run(false, |tx| {
            let mut found = nodes(tx)?
                .into_iter()
                .filter(|n| n.parent.as_deref() == Some(id))
                .collect::<Vec<_>>();
            found.sort_by(|a, b| (a.kind.as_str(), &a.id).cmp(&(b.kind.as_str(), &b.id)));
            Ok(found.into_iter().map(|n| n.id).collect())
        })
    }
    fn roots(&self) -> Result<Vec<String>> {
        self.run(false, |tx| {
            let mut found = nodes(tx)?
                .into_iter()
                .filter(|n| n.parent.is_none())
                .collect::<Vec<_>>();
            found.sort_by(|a, b| {
                (a.payload["run"].as_str().unwrap_or(""), &a.id)
                    .cmp(&(b.payload["run"].as_str().unwrap_or(""), &b.id))
            });
            Ok(found.into_iter().map(|n| n.id).collect())
        })
    }
    fn update_meta(&mut self, id: &str, patch: Value) -> Result<()> {
        self.run(true, |tx| patch_node(tx, id, &patch))
    }
    fn apply_batch(&mut self, nodes: Vec<Node>, patches: Vec<(String, Value)>) -> Result<()> {
        self.run(true, |tx| {
            for node in &nodes {
                put_node(tx, node)?;
            }
            for (id, patch) in &patches {
                patch_node(tx, id, patch)?;
            }
            Ok(())
        })
    }
    fn drop_nodes(&mut self, ids: &BTreeSet<String>) -> Result<()> {
        self.run(true, |tx| {
            for id in ids {
                tx.delete("nodes", id)?;
            }
            Ok(())
        })
    }
    fn compact(&mut self) -> Result<usize> {
        if self.read_only {
            return Err(Error::Invalid("store is read-only".into()));
        }
        Ok(0)
    }
    fn walk(&self, root: &str) -> Result<Vec<Node>> {
        self.run(false, |tx| {
            let nodes = nodes(tx)?
                .into_iter()
                .map(|n| (n.id.clone(), n))
                .collect::<BTreeMap<_, _>>();
            let mut children = BTreeMap::<String, Vec<String>>::new();
            for node in nodes.values() {
                if let Some(parent) = &node.parent {
                    children
                        .entry(parent.clone())
                        .or_default()
                        .push(node.id.clone());
                }
            }
            for ids in children.values_mut() {
                ids.sort_by(|a, b| (nodes[a].kind.as_str(), a).cmp(&(nodes[b].kind.as_str(), b)));
            }
            let mut pending = vec![root.to_owned()];
            let mut seen = BTreeSet::new();
            let mut ordered = Vec::new();
            while let Some(id) = pending.pop() {
                if !seen.insert(id.clone()) {
                    return Err(integrity("cycle in stored tree"));
                }
                ordered.push(
                    nodes
                        .get(&id)
                        .ok_or_else(|| Error::NotFound(id.clone()))?
                        .clone(),
                );
                if let Some(ids) = children.get(&id) {
                    pending.extend(ids.iter().rev().cloned());
                }
            }
            Ok(ordered)
        })
    }
}
impl<B: KvBackend> RecordingStore for TransactionalKvStore<B> {
    // Python's remote stores publish only completed records. Keep staging absent
    // and use the shared reservation ledger as the pre-dispatch durable fence.
    fn stage_pending(&mut self, _node: Node) -> Result<()> {
        if self.read_only {
            Err(Error::Invalid("store is read-only".into()))
        } else {
            Ok(())
        }
    }
    fn finalize(&mut self, node: Node) -> Result<()> {
        self.put(node)
    }
    fn supports_reservations(&self) -> bool {
        !self.read_only
    }
    fn reserve_budget(
        &mut self,
        id: &str,
        budgets: &[BudgetReservation],
        windows: &[WindowReservation],
        lease: f64,
    ) -> Result<Option<ReservationCheck>> {
        self.reserve(id, budgets, windows, lease).map(Some)
    }
    fn settle_budget(&mut self, id: &str, charges: &BTreeMap<String, Decimal>) -> Result<()> {
        self.settle(id, charges)
    }
    fn release_budget(&mut self, id: &str) -> Result<()> {
        self.release(id)
    }
    fn lease_renewer(&self) -> Option<LeaseRenewer> {
        if self.read_only {
            return None;
        }
        let backend = self.backend.clone();
        Some(Arc::new(move |id, lease| {
            let store = TransactionalKvStore {
                backend: backend.clone(),
                read_only: false,
            };
            store.renew(id, lease)
        }))
    }
}

fn active_amount(active: &[Value], kind: &str, scope: &str, meter: &str) -> Result<Decimal> {
    let mut total = Decimal::ZERO;
    for reservation in active {
        let details = reservation["details"]
            .as_array()
            .ok_or_else(|| integrity("invalid reservation details"))?;
        for d in details {
            if !d.is_object() {
                return Err(integrity("invalid reservation detail"));
            }
            if d["kind"] == json!(kind)
                && d["scope_id"] == json!(scope)
                && d["meter"] == json!(meter)
            {
                total = plus(total, dec(string(d, "amount")?)?)?;
            }
        }
    }
    Ok(total)
}
pub fn reserve_once(
    tx: &mut dyn KvTransaction,
    id: &str,
    budgets: &[BudgetReservation],
    windows: &[WindowReservation],
    lease: f64,
) -> Result<ReservationCheck> {
    let encoded = reservation_request(budgets, windows, lease)?;
    reserve_encoded_once(tx, id, budgets, windows, lease, &encoded, None)
}
fn reserve_encoded_once(
    tx: &mut dyn KvTransaction,
    id: &str,
    budgets: &[BudgetReservation],
    windows: &[WindowReservation],
    lease: f64,
    encoded: &(String, String),
    spelling: Option<&Value>,
) -> Result<ReservationCheck> {
    let (request, digest) = encoded;
    let now = tx.now();
    if let Some(raw) = tx.get("reservations", id)? {
        let current = object(&raw)?;
        if current["request_digest"] != json!(digest) {
            return Err(integrity(format!(
                "reservation retry changed request: {id}"
            )));
        }
        if string(&current, "state")? != "active" {
            return Err(integrity(format!(
                "reservation is already {}: {id}",
                string(&current, "state")?
            )));
        }
        if float(&current, "expires_at")? <= now {
            return Err(integrity(format!("reservation expired before retry: {id}")));
        }
        return Ok(ReservationCheck::default());
    }
    let mut active = Vec::new();
    for (_, raw) in tx.items("reservations")? {
        let v = object(&raw)?;
        if v["state"] == json!("active") && float(&v, "expires_at")? > now {
            active.push(v);
        }
    }
    let mut rows = Vec::new();
    for budget in budgets {
        for (meter, limit) in &budget.limits {
            if meter != "depth" {
                rows.push((budget, meter, *limit));
            }
        }
    }
    let mut sorted = rows.clone();
    sorted.sort_by(|a, b| (&a.0.scope_id, a.1).cmp(&(&b.0.scope_id, b.1)));
    for (budget, meter, limit) in sorted {
        let key = compound_key(&[&budget.scope_id, meter])?;
        let stored = tx.get("budget", &key)?;
        let previous = stored
            .as_deref()
            .map(dec)
            .transpose()?
            .unwrap_or(Decimal::ZERO);
        let baseline = *budget.baseline.get(meter).unwrap_or(&Decimal::ZERO);
        let settled = if baseline > previous {
            baseline
        } else {
            previous
        };
        if stored.is_none() || previous != settled {
            let value = if baseline > previous {
                spelling
                    .and_then(|v| {
                        crate::decimal_wire::budget_spelling(v, &budget.scope_id, meter, "baseline")
                    })
                    .map(str::to_owned)
                    .unwrap_or_else(|| decimal_string(settled))
            } else {
                decimal_string(settled)
            };
            tx.put("budget", &key, &value)?;
        }
        let reserved = active_amount(&active, "budget", &budget.scope_id, meter)?;
        let amount = *budget.estimates.get(meter).unwrap_or(&Decimal::ZERO);
        let remaining = minus(minus(limit, settled)?, reserved)?;
        if amount > remaining {
            return Ok(ReservationCheck {
                ok: false,
                meter: Some(meter.clone()),
                requested: amount,
                remaining,
                ..Default::default()
            });
        }
    }
    let mut events = Vec::new();
    for (key, raw) in tx.items("window-events")? {
        let event = object(&raw)?;
        if float(&event, "settled_at")? <= now - float(&event, "window_seconds")? {
            tx.delete("window-events", &key)?;
        } else {
            events.push(event);
        }
    }
    let mut sorted = windows.iter().collect::<Vec<_>>();
    sorted.sort_by(|a, b| a.ledger_key.cmp(&b.ledger_key));
    for w in sorted {
        let mut settled = Decimal::ZERO;
        for event in &events {
            if event["scope_id"] == json!(w.ledger_key)
                && event["meter"] == json!(w.meter)
                && float(event, "settled_at")? > now - w.window_seconds
            {
                settled = plus(settled, dec(string(event, "amount")?)?)?;
            }
        }
        let reserved = active_amount(&active, "window", &w.ledger_key, &w.meter)?;
        let remaining = minus(minus(w.limit, settled)?, reserved)?;
        if w.amount > remaining {
            return Ok(ReservationCheck {
                ok: false,
                reason: "window".into(),
                meter: Some(w.meter.clone()),
                requested: w.amount,
                remaining,
                window_seconds: Some(w.window_seconds),
            });
        }
    }
    let mut details = Vec::new();
    for (b, meter, _) in rows {
        let amount = spelling
            .and_then(|v| crate::decimal_wire::budget_spelling(v, &b.scope_id, meter, "estimates"))
            .map(str::to_owned)
            .unwrap_or_else(|| decimal_string(*b.estimates.get(meter).unwrap_or(&Decimal::ZERO)));
        details.push(json!({"kind":"budget","scope_id":b.scope_id,"meter":meter,"amount":amount}));
    }
    for w in windows {
        let amount = spelling
            .and_then(|v| crate::decimal_wire::window_spelling(v, &w.ledger_key, "amount"))
            .map(str::to_owned)
            .unwrap_or_else(|| decimal_string(w.amount));
        details.push(json!({"kind":"window","scope_id":w.ledger_key,"meter":w.meter,"amount":amount,"window_seconds":w.window_seconds}));
    }
    let expires_at = now + lease;
    if !expires_at.is_finite() {
        return Err(integrity("reservation expiry exceeds finite server time"));
    }
    tx.put("reservations",id,&text(&json!({"request_digest":digest,"request":request,"state":"active","charges_digest":null,"charges":null,"expires_at":expires_at,"created_at":now,"completed_at":null,"details":details}))?)?;
    Ok(ReservationCheck::default())
}
pub fn settle_once(
    tx: &mut dyn KvTransaction,
    id: &str,
    charges: &BTreeMap<String, Decimal>,
) -> Result<()> {
    let encoded = reservation_charges(charges)?;
    settle_encoded_once(tx, id, charges, &encoded, None)
}
fn settle_encoded_once(
    tx: &mut dyn KvTransaction,
    id: &str,
    charges: &BTreeMap<String, Decimal>,
    encoded: &(String, String),
    spelling: Option<&Value>,
) -> Result<()> {
    let (charges_text, digest) = encoded;
    let mut current = object(
        &tx.get("reservations", id)?
            .ok_or_else(|| integrity(format!("unknown reservation: {id}")))?,
    )?;
    let state = string(&current, "state")?;
    if state == "settled" {
        if current["charges_digest"] != json!(digest) {
            return Err(integrity(format!(
                "reservation retry used different charges: {id}"
            )));
        }
        return Ok(());
    }
    if state != "active" {
        return Err(integrity(format!("reservation is already {state}: {id}")));
    }
    let details = current["details"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or_else(|| integrity(format!("reservation details are missing: {id}")))?;
    let now = tx.now();
    for (index, d) in details.iter().enumerate() {
        let kind = string(d, "kind")?;
        let scope = string(d, "scope_id")?;
        let meter = string(d, "meter")?;
        let actual = *charges.get(meter).unwrap_or(&Decimal::ZERO);
        if kind == "budget" {
            let key = compound_key(&[scope, meter])?;
            let stored = tx
                .get("budget", &key)?
                .ok_or_else(|| integrity("budget state missing during settlement"))?;
            dec(&stored)?;
            let actual_text = spelling
                .and_then(|v| v.get(meter))
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| decimal_string(actual));
            tx.put(
                "budget",
                &key,
                &crate::decimal_wire::add_text(&stored, &actual_text)?,
            )?;
        } else if kind == "window" {
            if actual != Decimal::ZERO {
                let amount = spelling
                    .and_then(|v| v.get(meter))
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .unwrap_or_else(|| decimal_string(actual));
                tx.put("window-events",&compound_key(&[id,&index.to_string()])?,&text(&json!({"scope_id":scope,"meter":meter,"amount":amount,"settled_at":now,"window_seconds":float(d,"window_seconds")?}))?)?;
            }
        } else {
            return Err(integrity(format!("invalid reservation kind: {kind}")));
        }
    }
    current["state"] = json!("settled");
    current["charges_digest"] = json!(digest);
    current["charges"] = json!(charges_text);
    current["completed_at"] = json!(now);
    tx.put("reservations", id, &text(&current)?)
}
pub fn release_once(tx: &mut dyn KvTransaction, id: &str) -> Result<()> {
    let Some(raw) = tx.get("reservations", id)? else {
        return Ok(());
    };
    let mut current = object(&raw)?;
    let state = string(&current, "state")?;
    if state == "released" {
        return Ok(());
    }
    if state != "active" {
        return Err(integrity(format!("reservation is already {state}: {id}")));
    }
    current["state"] = json!("released");
    current["completed_at"] = json!(tx.now());
    tx.put("reservations", id, &text(&current)?)
}
pub fn renew_once(tx: &mut dyn KvTransaction, id: &str, lease: f64) -> Result<bool> {
    valid_seconds(lease)?;
    let Some(raw) = tx.get("reservations", id)? else {
        return Ok(false);
    };
    let mut current = object(&raw)?;
    let now = tx.now();
    if current["state"] != json!("active") || float(&current, "expires_at")? <= now {
        return Ok(false);
    }
    let expires_at = now + lease;
    if !expires_at.is_finite() {
        return Err(integrity("reservation expiry exceeds finite server time"));
    }
    current["expires_at"] = json!(expires_at);
    tx.put("reservations", id, &text(&current)?)?;
    Ok(true)
}
