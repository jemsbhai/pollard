use crate::identity::{safe_amount, MAX_SAFE_INTEGER};
use crate::{
    canonical_bytes, digest_payload, verify, ActionSpec, Decision, Error, MemoryStore, Node,
    NodeKind, Policy, PolicyContext, RecordingStore, Registry, Result,
};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::cell::{Cell, Ref, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;
use std::time::Instant;

/// Additional budgets and charges, named exactly as their configured meter.
pub type MeterCharges = BTreeMap<String, f64>;
pub type MeterBudget = BTreeMap<String, f64>;
pub type NodeCallback = Rc<dyn Fn(&Node) -> Result<()>>;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RunReport {
    pub spent: MeterCharges,
    pub avoided: MeterCharges,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayMode {
    Record,
    Hybrid,
    Replay,
}

/// Integer budgets. Depth is absolute ancestry depth; root depth is zero.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Budget {
    pub steps: Option<u64>,
    pub tokens: Option<u64>,
    pub depth: Option<u64>,
}
impl Budget {
    pub fn validate(&self) -> Result<()> {
        for (name, value) in [
            ("steps", self.steps),
            ("tokens", self.tokens),
            ("depth", self.depth),
        ] {
            if let Some(n) = value {
                safe_amount(n, name)?;
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Charges {
    pub steps: u64,
    pub tokens: u64,
}
impl Charges {
    fn add(&mut self, other: Self) -> Result<()> {
        self.steps = self
            .steps
            .checked_add(other.steps)
            .ok_or_else(|| Error::Integrity("step total overflow".into()))?;
        self.tokens = self
            .tokens
            .checked_add(other.tokens)
            .ok_or_else(|| Error::Integrity("token total overflow".into()))?;
        safe_amount(self.steps, "step total")?;
        safe_amount(self.tokens, "token total")?;
        Ok(())
    }
}

/// Supply a conservative total input + output token estimate for token gates.
#[derive(Debug, Clone, Copy, Default)]
pub struct CallOptions {
    pub attempt: u64,
    pub estimated_tokens: Option<u64>,
}

/// Identity for one deliberate live comparison. Supply an observation ID once;
/// reusing it at the same recording cursor is rejected before dispatch.
#[derive(Debug, Clone)]
pub struct RevalidationOptions {
    pub observation_id: String,
    pub call: CallOptions,
    pub live_payload: Option<Value>,
    /// Retain original chunks when using streaming revalidation.
    pub keep_chunks: bool,
}
impl RevalidationOptions {
    pub fn new(observation_id: impl Into<String>) -> Self {
        Self {
            observation_id: observation_id.into(),
            call: CallOptions::default(),
            live_payload: None,
            keep_chunks: false,
        }
    }
}

struct StoreCell {
    inner: RefCell<Box<dyn RecordingStore>>,
    busy: Cell<bool>,
    index: RefCell<RuntimeIndex>,
}
#[derive(Default)]
struct RuntimeIndex {
    revision: Option<crate::StoreRevision>,
    verified: BTreeSet<String>,
    ancestry: BTreeMap<String, (Option<String>, String, u64)>,
    charges: BTreeMap<String, MeterCharges>,
    totals: BTreeMap<String, MeterCharges>,
}
impl RuntimeIndex {
    fn remember(&mut self, node: &Node) -> Result<()> {
        let info = if let Some(parent) = &node.parent {
            self.ancestry
                .get(parent)
                .map(|(_, root, depth)| (node.parent.clone(), root.clone(), depth + 1))
        } else {
            Some((None, node.id.clone(), 0))
        };
        if let Some(info) = info {
            self.ancestry.insert(node.id.clone(), info);
        }
        self.charges
            .insert(node.id.clone(), meter_charges_from_meta(&node.meta)?);
        Ok(())
    }
    fn descends(&self, node: &str, anchor: &str) -> bool {
        let Some((_, root, _)) = self.ancestry.get(node) else {
            return false;
        };
        if root == anchor {
            return true;
        }
        let mut current = Some(node);
        while let Some(id) = current {
            if id == anchor {
                return true;
            }
            current = self
                .ancestry
                .get(id)
                .and_then(|(parent, _, _)| parent.as_deref());
        }
        false
    }
}
/// Share this handle across runtimes to share records and a reentrancy lock.
/// This synchronous API is intentionally single-threaded.
#[derive(Clone)]
pub struct SharedStore(Rc<StoreCell>);
impl SharedStore {
    pub fn new(store: impl RecordingStore + 'static) -> Self {
        Self(Rc::new(StoreCell {
            inner: RefCell::new(Box::new(store)),
            busy: Cell::new(false),
            index: RefCell::new(RuntimeIndex::default()),
        }))
    }
    pub fn borrow(&self) -> Ref<'_, dyn RecordingStore> {
        Ref::map(self.0.inner.borrow(), |s| s.as_ref())
    }
    pub(crate) fn lock(&self) -> Result<OperationGuard> {
        if self.0.busy.replace(true) {
            return Err(Error::Busy);
        }
        Ok(OperationGuard(self.clone()))
    }
    fn synchronize_index(&self) -> bool {
        let revision = self.borrow().cache_revision();
        let mut index = self.0.index.borrow_mut();
        if index.revision != revision || revision.is_none() {
            *index = RuntimeIndex {
                revision,
                ..Default::default()
            };
        }
        revision.is_some()
    }
}
pub(crate) struct OperationGuard(SharedStore);
impl Drop for OperationGuard {
    fn drop(&mut self) {
        self.0 .0.busy.set(false);
    }
}

#[derive(Clone)]
pub struct Runtime {
    store: SharedStore,
    mode: ReplayMode,
    registry: Option<Registry>,
    policies: Vec<Policy>,
    refuse_duplicate_recordings: bool,
    dry_run: bool,
    meters: Vec<Rc<dyn crate::Meter>>,
    core_meters: bool,
    on_node: Option<NodeCallback>,
    callback_errors: Rc<RefCell<Vec<String>>>,
    cleanup_errors: Rc<RefCell<Vec<String>>>,
    reservation_lease_seconds: f64,
}
impl Runtime {
    pub fn new(store: impl RecordingStore + 'static, mode: ReplayMode) -> Self {
        Self::from_shared(SharedStore::new(store), mode)
    }
    pub fn memory(mode: ReplayMode) -> Self {
        Self::new(MemoryStore::new(), mode)
    }
    pub fn from_shared(store: SharedStore, mode: ReplayMode) -> Self {
        Self {
            store,
            mode,
            registry: None,
            policies: Vec::new(),
            refuse_duplicate_recordings: false,
            dry_run: false,
            meters: vec![Rc::new(crate::WallClockMeter)],
            core_meters: true,
            on_node: None,
            callback_errors: Rc::new(RefCell::new(Vec::new())),
            cleanup_errors: Rc::new(RefCell::new(Vec::new())),
            reservation_lease_seconds: 60.0,
        }
    }
    pub fn with_registry(mut self, registry: Registry) -> Self {
        self.registry = Some(registry);
        self
    }
    pub fn with_policy(mut self, policy: Policy) -> Self {
        self.policies.push(policy);
        self
    }
    pub fn with_refuse_duplicate_recordings(mut self, refuse: bool) -> Self {
        self.refuse_duplicate_recordings = refuse;
        self
    }
    pub fn with_dry_run(mut self, dry_run: bool) -> Self {
        self.dry_run = dry_run;
        self
    }
    /// Add or replace one meter by name. Built-in steps/tokens remain enabled.
    pub fn with_meter(mut self, meter: impl crate::Meter + 'static) -> Self {
        self.meters.retain(|m| m.name() != meter.name());
        self.meters.push(Rc::new(meter));
        self
    }
    /// Replace the meter list. An empty list restores default meters, as in Python.
    pub fn with_meters(mut self, meters: Vec<Rc<dyn crate::Meter>>) -> Self {
        if meters.is_empty() {
            self.meters = vec![Rc::new(crate::WallClockMeter)];
            self.core_meters = true;
        } else {
            self.meters = meters;
            self.core_meters = false;
        }
        self
    }
    pub fn with_reservation_lease_seconds(mut self, seconds: f64) -> Result<Self> {
        if !seconds.is_finite() || seconds <= 0.0 {
            return Err(Error::Invalid(
                "reservation lease seconds must be positive and finite".into(),
            ));
        }
        self.reservation_lease_seconds = seconds;
        Ok(self)
    }
    pub fn with_on_node(mut self, callback: NodeCallback) -> Self {
        self.on_node = Some(callback);
        self
    }
    /// Observer errors do not roll back an already committed recording.
    pub fn callback_errors(&self) -> Vec<String> {
        self.callback_errors.borrow().clone()
    }
    /// Measurement cleanup failures, including secondary errors while preserving
    /// a provider's primary error and errors observed during async cancellation.
    pub fn cleanup_errors(&self) -> Vec<String> {
        self.cleanup_errors.borrow().clone()
    }
    fn notify_node(&self, node: &Node) {
        if let Some(callback) = &self.on_node {
            if let Err(error) = callback(node) {
                self.callback_errors.borrow_mut().push(error.to_string());
            }
        }
    }
    fn write_node(&self, node: Node, finalize: bool) -> Result<()> {
        let trusted = self.store.synchronize_index();
        let before = self.store.0.index.borrow().revision;
        if finalize {
            self.store.0.inner.borrow_mut().finalize(node.clone())?;
        } else if node.meta.get("state").and_then(Value::as_str) == Some("pending") {
            self.store
                .0
                .inner
                .borrow_mut()
                .stage_pending(node.clone())?;
        } else {
            self.store.0.inner.borrow_mut().put(node.clone())?;
        }
        if trusted {
            self.refresh_index(&node.id, before.expect("cache token"))?;
        }
        Ok(())
    }
    fn update_meta(&self, id: &str, patch: Value) -> Result<()> {
        let trusted = self.store.synchronize_index();
        let before = self.store.0.index.borrow().revision;
        self.store.0.inner.borrow_mut().update_meta(id, patch)?;
        if trusted {
            self.refresh_index(id, before.expect("cache token"))?;
        }
        Ok(())
    }
    fn refresh_index(&self, id: &str, before: crate::StoreRevision) -> Result<()> {
        let after = self.store.borrow().cache_revision();
        if after.is_none() || after.is_some_and(|token| token.external != before.external) {
            self.store.synchronize_index();
            return Ok(());
        }
        let node = self.store.borrow().get(id)?;
        if self.store.borrow().cache_revision() != after {
            self.store.synchronize_index();
            return Ok(());
        }
        let mut index = self.store.0.index.borrow_mut();
        let old = index.charges.get(id).cloned().unwrap_or_default();
        index.remember(&node)?;
        let new = index.charges.get(id).cloned().unwrap_or_default();
        let anchors: Vec<_> = index
            .totals
            .keys()
            .filter(|anchor| index.descends(id, anchor))
            .cloned()
            .collect();
        for anchor in anchors {
            let total = index.totals.get_mut(&anchor).expect("known anchor");
            for (name, amount) in &old {
                if let Some(value) = total.get_mut(name) {
                    *value = decimal_subtract(*value, *amount)?.max(0.0);
                }
            }
            add_amounts(total, &new)?;
            total.retain(|_, amount| *amount != 0.0);
        }
        if node.meta.get("state").and_then(Value::as_str) == Some("pending") {
            index.verified.remove(id);
        }
        // Preserve the sampled token. A commit after this check is detected on
        // the next operation instead of accidentally accepting stale ancestors.
        index.revision = after;
        Ok(())
    }
    pub fn shared_store(&self) -> SharedStore {
        self.store.clone()
    }
    pub fn store(&self) -> Ref<'_, dyn RecordingStore> {
        self.store.borrow()
    }
    pub fn mode(&self) -> ReplayMode {
        self.mode
    }

    pub fn run(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        attempt: u64,
    ) -> Result<Run> {
        let _guard = self.store.lock()?;
        if let Some(b) = &budget {
            b.validate()?;
        }
        let label = label.into();
        let candidate = Node::make(
            NodeKind::Root,
            None,
            attempt,
            json!({"run":label}),
            None,
            json!({}),
        )?;
        let root = self.structural(candidate)?;
        if let Some(registry) = &self.registry {
            let existing = root.meta.get("registry_digest");
            if existing.is_some_and(|d| d.as_str() != Some(registry.registry_digest())) {
                return Err(Error::Integrity(
                    "run is already bound to a different registry".into(),
                ));
            }
            if existing.is_none() {
                if self.mode == ReplayMode::Replay {
                    return Err(Error::Integrity(
                        "replay root is not bound to registry".into(),
                    ));
                }
                self.update_meta(
                    &root.id,
                    json!({"registry_digest":registry.registry_digest()}),
                )?;
            }
        }
        let scopes = budget
            .into_iter()
            .map(|budget| Scope {
                budget,
                anchor: root.id.clone(),
            })
            .collect();
        Ok(Run {
            runtime: self.clone(),
            root_id: root.id.clone(),
            cursor_id: root.id,
            label,
            scopes,
            pending: BTreeMap::new(),
            avoided: Charges::default(),
            avoided_meters: BTreeMap::new(),
            meter_scopes: Vec::new(),
        })
    }

    pub fn run_with_meters(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        meter_budget: MeterBudget,
        attempt: u64,
    ) -> Result<Run> {
        validate_amounts(&meter_budget)?;
        let mut run = self.run(label, budget, attempt)?;
        run.meter_scopes.push((run.root_id.clone(), meter_budget));
        Ok(run)
    }

    /// Resume at the deepest non-pruned leaf; equal depths choose the lower ID.
    pub fn resume(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        attempt: u64,
    ) -> Result<Run> {
        let label = label.into();
        let root = Node::make(
            NodeKind::Root,
            None,
            attempt,
            json!({"run":label}),
            None,
            json!({}),
        )?;
        if !self.store.borrow().try_exists(&root.id)? {
            return Err(Error::NotFound(root.id));
        }
        let mut run = self.run(label, budget, attempt)?;
        let mut best = (0, run.root_id.clone());
        for node in self.store.borrow().walk(&run.root_id)? {
            if node.meta.get("pruned") == Some(&json!(true)) {
                continue;
            }
            let mut active_child = false;
            for child in self.store.borrow().children(&node.id)? {
                if self.store.borrow().get(&child)?.meta.get("pruned") != Some(&json!(true)) {
                    active_child = true;
                    break;
                }
            }
            if active_child {
                continue;
            }
            self.verified(&node.id)?;
            let depth = run.depth_at(&node.id)?;
            if depth > best.0 || (depth == best.0 && node.id < best.1) {
                best = (depth, node.id);
            }
        }
        run.cursor_id = best.1;
        Ok(run)
    }

    pub fn resume_with_meters(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        meter_budget: MeterBudget,
        attempt: u64,
    ) -> Result<Run> {
        validate_amounts(&meter_budget)?;
        let mut run = self.resume(label, budget, attempt)?;
        run.meter_scopes.push((run.root_id.clone(), meter_budget));
        Ok(run)
    }

    fn verified(&self, id: &str) -> Result<Node> {
        for _ in 0..3 {
            if !self.store.synchronize_index() {
                let store = self.store.borrow();
                let report = verify(&*store, id);
                if !report.ok {
                    return Err(Error::Integrity(
                        report
                            .findings
                            .iter()
                            .map(|f| format!("{}: {}", f.node_id, f.message))
                            .collect::<Vec<_>>()
                            .join("; "),
                    ));
                }
                return store.get(id);
            }
            let revision = self.store.0.index.borrow().revision;
            let mut nodes = Vec::new();
            let mut seen = BTreeSet::new();
            let mut current = Some(id.to_owned());
            while let Some(next) = current {
                if !seen.insert(next.clone()) {
                    return Err(Error::Integrity("cycle in ancestry".into()));
                }
                if next != id && self.store.0.index.borrow().verified.contains(&next) {
                    break;
                }
                let node = self.store.borrow().get(&next)?;
                if node.id != next {
                    return Err(Error::Integrity("lookup key differs from node id".into()));
                }
                node.validate()?;
                current = node.parent.clone();
                nodes.push(node);
            }
            // External commits can race individual SELECTs. Never cache or use a
            // prefix assembled across revisions; repeated contention fails closed.
            if self.store.borrow().cache_revision() != revision {
                self.store.synchronize_index();
                continue;
            }
            let result = nodes.first().expect("requested node checked").clone();
            let mut index = self.store.0.index.borrow_mut();
            for node in nodes.into_iter().rev() {
                index.remember(&node)?;
                if node.meta.get("state").and_then(Value::as_str) != Some("pending") {
                    index.verified.insert(node.id);
                }
            }
            return Ok(result);
        }
        Err(Error::Integrity(
            "recording changed repeatedly during verification".into(),
        ))
    }

    fn structural(&self, candidate: Node) -> Result<Node> {
        if self.store.borrow().try_exists(&candidate.id)? {
            return self.verified(&candidate.id);
        }
        if self.mode == ReplayMode::Replay {
            return Err(Error::MissingRecording(candidate.id));
        }
        self.write_node(candidate.clone(), false)?;
        self.notify_node(&candidate);
        Ok(candidate)
    }
}

#[derive(Clone)]
struct Scope {
    budget: Budget,
    anchor: String,
}
#[derive(Clone)]
struct Pending {
    parent: String,
    payload: Value,
    args: Value,
    spec: ActionSpec,
    options: CallOptions,
}

pub struct Run {
    runtime: Runtime,
    root_id: String,
    cursor_id: String,
    label: String,
    scopes: Vec<Scope>,
    pending: BTreeMap<String, Pending>,
    avoided: Charges,
    avoided_meters: MeterCharges,
    meter_scopes: Vec<(String, MeterBudget)>,
}
impl Run {
    pub fn root_id(&self) -> &str {
        &self.root_id
    }
    pub fn cursor_id(&self) -> &str {
        &self.cursor_id
    }
    pub fn cursor(&self) -> Result<Node> {
        self.runtime.store.borrow().get(&self.cursor_id)
    }
    pub fn spent(&self) -> Result<Charges> {
        Ok(self.accounting(&self.root_id)?.0)
    }
    pub fn avoided(&self) -> Charges {
        self.avoided
    }
    pub fn report(&self) -> Result<RunReport> {
        Ok(RunReport {
            spent: self.meter_accounting(&self.root_id)?,
            avoided: self.avoided_meters.clone(),
        })
    }
    pub(crate) fn operation_lock(&self) -> Result<OperationGuard> {
        self.runtime.store.lock()
    }
    pub(crate) fn ensure_unfenced_tool(&self) -> Result<()> {
        if self.runtime.registry.is_some() {
            return Err(Error::Invalid(
                "use registered_tool_call with a registry".into(),
            ));
        }
        Ok(())
    }
    pub fn prune(&mut self) -> Result<()> {
        let _guard = self.operation_lock()?;
        if self.runtime.mode == ReplayMode::Replay {
            return Err(Error::Invalid("replay mode is read-only".into()));
        }
        self.runtime
            .update_meta(&self.cursor_id, json!({"pruned":true}))
    }
    pub fn rollback_steps(&mut self, steps: usize) -> Result<Node> {
        let mut target = self.cursor_id.clone();
        for _ in 0..steps {
            match self.runtime.store.borrow().get(&target)?.parent {
                Some(parent) => target = parent,
                None => break,
            }
        }
        self.rollback(&target)
    }

    pub fn model_call<F>(
        &mut self,
        payload: Value,
        options: CallOptions,
        handler: F,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<Value>,
    {
        let _guard = self.runtime.store.lock()?;
        self.call(
            NodeKind::ModelCall,
            payload.clone(),
            payload,
            options,
            handler,
        )
    }

    /// Compare a stored model result with a separately recorded live observation.
    /// The continuation cursor returns to the original recording after comparison.
    pub fn revalidate_model_call<F>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        handler: F,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<Value>,
    {
        self.revalidate_model_call_with_comparator(
            payload,
            contract,
            options,
            &crate::NormalizedModelComparator,
            handler,
        )
    }

    pub fn revalidate_model_call_with_comparator<F>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        comparator: &dyn crate::RevalidationComparator,
        handler: F,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<Value>,
    {
        let _guard = self.operation_lock()?;
        let prepared = self.prepare_revalidation(payload, contract, options, comparator)?;
        let dispatch = self.prepare_observation_dispatch(&prepared)?;
        let live = self.finish_call(dispatch, handler(prepared.live_payload.clone()))?;
        self.finish_revalidation(prepared, live, comparator)
    }

    pub async fn revalidate_model_call_async<F, Fut>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        handler: F,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Fut,
        Fut: std::future::Future<Output = Result<Value>>,
    {
        self.revalidate_model_call_async_with_comparator(
            payload,
            contract,
            options,
            &crate::NormalizedModelComparator,
            handler,
        )
        .await
    }

    pub async fn revalidate_model_call_async_with_comparator<F, Fut>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        comparator: &dyn crate::RevalidationComparator,
        handler: F,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Fut,
        Fut: std::future::Future<Output = Result<Value>>,
    {
        let _guard = self.operation_lock()?;
        let prepared = self.prepare_revalidation(payload, contract, options, comparator)?;
        let dispatch = self.prepare_observation_dispatch(&prepared)?;
        let outcome = handler(prepared.live_payload.clone()).await;
        let live = self.finish_call(dispatch, outcome)?;
        self.finish_revalidation(prepared, live, comparator)
    }

    /// Stream a live observation, then compare it without replacing the golden recording.
    pub fn revalidate_model_stream<F, I, O>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        handler: F,
        on_delta: O,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<I>,
        I: IntoIterator<Item = Result<Value>>,
        O: FnMut(&Value) -> Result<()>,
    {
        self.revalidate_model_stream_with_comparator(
            payload,
            contract,
            options,
            &crate::NormalizedModelComparator,
            handler,
            on_delta,
        )
    }

    pub fn revalidate_model_stream_with_comparator<F, I, O>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        comparator: &dyn crate::RevalidationComparator,
        handler: F,
        on_delta: O,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<I>,
        I: IntoIterator<Item = Result<Value>>,
        O: FnMut(&Value) -> Result<()>,
    {
        let keep_chunks = options.keep_chunks;
        self.revalidate_model_call_with_comparator(
            payload,
            contract,
            options,
            comparator,
            |live_payload| crate::consume_stream(handler(live_payload)?, keep_chunks, on_delta),
        )
    }

    /// Construct the asynchronous pull stream only after governance gates pass.
    pub async fn revalidate_model_stream_async<F, N, Fut, O, OFut>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        handler: F,
        on_delta: O,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<N>,
        N: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<Option<Value>>>,
        O: FnMut(Value) -> OFut,
        OFut: std::future::Future<Output = Result<()>>,
    {
        self.revalidate_model_stream_async_with_comparator(
            payload,
            contract,
            options,
            &crate::NormalizedModelComparator,
            handler,
            on_delta,
        )
        .await
    }

    pub async fn revalidate_model_stream_async_with_comparator<F, N, Fut, O, OFut>(
        &mut self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        comparator: &dyn crate::RevalidationComparator,
        handler: F,
        on_delta: O,
    ) -> Result<crate::RevalidationReport>
    where
        F: FnOnce(Value) -> Result<N>,
        N: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<Option<Value>>>,
        O: FnMut(Value) -> OFut,
        OFut: std::future::Future<Output = Result<()>>,
    {
        let keep_chunks = options.keep_chunks;
        self.revalidate_model_call_async_with_comparator(
            payload,
            contract,
            options,
            comparator,
            |live_payload| async move {
                crate::consume_stream_async(handler(live_payload)?, keep_chunks, on_delta).await
            },
        )
        .await
    }

    fn prepare_observation_dispatch(
        &mut self,
        prepared: &PreparedRevalidation,
    ) -> Result<PendingDispatch> {
        match self.prepare_call_metered(
            NodeKind::ModelCall,
            prepared.observation_payload.clone(),
            CallOptions {
                attempt: 0,
                estimated_tokens: prepared.options.call.estimated_tokens,
            },
            Some(prepared.live_payload.clone()),
        )? {
            CallPreparation::Recorded(_) => unreachable!("new observation identity"),
            CallPreparation::Dispatch(dispatch) => Ok(dispatch),
        }
    }

    fn prepare_revalidation(
        &self,
        payload: Value,
        contract: &crate::ReplayContract,
        options: RevalidationOptions,
        comparator: &dyn crate::RevalidationComparator,
    ) -> Result<PreparedRevalidation> {
        if self.runtime.mode != ReplayMode::Record || self.runtime.dry_run {
            return Err(Error::Invalid(
                "live revalidation requires record mode without dry_run".into(),
            ));
        }
        contract.validate()?;
        if comparator.name().trim().is_empty() {
            return Err(Error::Invalid("comparator name cannot be empty".into()));
        }
        let candidate = Node::make(
            NodeKind::ModelCall,
            Some(&self.cursor_id),
            options.call.attempt,
            payload.clone(),
            None,
            json!({}),
        )?;
        if !self.runtime.store.borrow().try_exists(&candidate.id)? {
            return Err(Error::MissingRecording(candidate.id));
        }
        let recorded = self.runtime.verified(&candidate.id)?;
        if !recorded.result.as_ref().is_some_and(Value::is_object)
            || recorded.result_digest.is_none()
        {
            return Err(Error::Integrity(
                "recorded model result is not a replayable object".into(),
            ));
        }
        let recorded_contract = crate::extract_replay_contract(&recorded.payload)?;
        let live_contract = contract.to_value();
        let live_payload = options.live_payload.clone().unwrap_or(payload);
        if options.live_payload.is_some() {
            if let Some(bound) = crate::extract_replay_contract(&live_payload)? {
                if bound != live_contract {
                    return Err(Error::Invalid(
                        "live payload replay contract does not match live contract".into(),
                    ));
                }
            }
        }
        let observation_payload = crate::make_revalidation_payload(
            &live_payload,
            &options.observation_id,
            &recorded,
            contract,
            comparator.name(),
        )?;
        let observation = Node::make(
            NodeKind::ModelCall,
            Some(&self.cursor_id),
            0,
            observation_payload.clone(),
            None,
            json!({}),
        )?;
        if self.runtime.store.borrow().try_exists(&observation.id)? {
            return Err(Error::Integrity(format!(
                "revalidation observation already exists: {}",
                options.observation_id
            )));
        }
        Ok(PreparedRevalidation {
            recorded,
            recorded_contract,
            live_contract,
            live_payload,
            observation_payload,
            options,
        })
    }

    fn finish_revalidation(
        &mut self,
        prepared: PreparedRevalidation,
        live: Node,
        comparator: &dyn crate::RevalidationComparator,
    ) -> Result<crate::RevalidationReport> {
        let PreparedRevalidation {
            recorded,
            recorded_contract,
            live_contract,
            options,
            ..
        } = prepared;
        let outcome = (|| -> Result<crate::RevalidationReport> {
            let comparison = comparator.compare(
                recorded.result.as_ref().expect("validated recorded result"),
                live.result.as_ref().expect("validated live result"),
            )?;
            comparison.validate()?;
            let exact_match = recorded.result_text == live.result_text;
            let mut evidence_payload = json!({"event":"model_revalidation","format":"pollard/revalidation/v1",
                "observation_id":options.observation_id,"recorded_node_id":recorded.id,"live_node_id":live.id,
                "recorded_result_digest":recorded.result_digest,"live_result_digest":live.result_digest,
                "comparator":comparator.name(),"comparison":comparison.to_value(),"exact_match":exact_match,
                "live_contract":live_contract});
            if let Some(bound) = &recorded_contract {
                evidence_payload["recorded_contract"] = bound.clone();
            }
            let evidence = self.runtime.structural(Node::make(
                NodeKind::Note,
                Some(&live.id),
                0,
                evidence_payload,
                None,
                json!({}),
            )?)?;
            Ok(crate::RevalidationReport {
                observation_id: options.observation_id.clone(),
                recorded_node_id: recorded.id.clone(),
                live_node_id: live.id.clone(),
                evidence_node_id: evidence.id,
                comparator: comparator.name().into(),
                matched: comparison.matched,
                exact_match,
                recorded_result_digest: recorded.result_digest.clone().expect("validated digest"),
                live_result_digest: live.result_digest.clone().expect("live digest"),
                difference_paths: comparison.difference_paths,
                differences_truncated: comparison.truncated,
                recorded_contract,
                live_contract,
                charges: meter_charges_from_meta(&live.meta)?,
            })
        })();
        if outcome.is_err() {
            let failure = Node::make(
                NodeKind::Note,
                Some(&live.id),
                0,
                json!({
                "event":"model_revalidation_comparison_failed","format":"pollard/revalidation/v1",
                "observation_id":options.observation_id,"recorded_node_id":recorded.id,"live_node_id":live.id,
                "recorded_result_digest":recorded.result_digest,"live_result_digest":live.result_digest,
                "comparator":comparator.name(),"error_type":"ComparatorError"}),
                None,
                json!({}),
            );
            if let Ok(failure) = failure {
                let _ = self.runtime.structural(failure);
            }
        }
        self.cursor_id = recorded.id;
        outcome
    }

    /// Unfenced tool callback. Disallowed when a registry is attached.
    pub fn tool_call<F>(
        &mut self,
        name: &str,
        args: Value,
        options: CallOptions,
        handler: F,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<Value>,
    {
        let _guard = self.runtime.store.lock()?;
        if self.runtime.registry.is_some() {
            return Err(Error::Invalid(
                "use registered_tool_call with a registry".into(),
            ));
        }
        if !args.is_object() {
            return Err(Error::Invalid("tool args must be object".into()));
        }
        let payload = json!({"tool":name,"args":args});
        self.call(
            NodeKind::ToolCall,
            payload.clone(),
            payload,
            options,
            handler,
        )
    }

    pub fn registered_tool_call(
        &mut self,
        name: &str,
        version: Option<&str>,
        args: Value,
        options: CallOptions,
    ) -> Result<Node> {
        let _guard = self.operation_lock()?;
        match self.prepare_registered_tool(name, version, args, options, false)? {
            RegisteredPreparation::Ready(node) => Ok(node),
            RegisteredPreparation::Dispatch {
                dispatch,
                args,
                handler: PreparedHandler::Sync(handler),
            } => self.finish_call(dispatch, handler(args)),
            RegisteredPreparation::Dispatch {
                handler: PreparedHandler::Async(_),
                ..
            } => unreachable!("sync preparation rejects async-only handlers"),
        }
    }

    pub async fn registered_tool_call_async(
        &mut self,
        name: &str,
        version: Option<&str>,
        args: Value,
        options: CallOptions,
    ) -> Result<Node> {
        let _guard = self.operation_lock()?;
        match self.prepare_registered_tool(name, version, args, options, true)? {
            RegisteredPreparation::Ready(node) => Ok(node),
            RegisteredPreparation::Dispatch {
                dispatch,
                args,
                handler,
            } => {
                let outcome = match handler {
                    PreparedHandler::Sync(handler) => handler(args),
                    PreparedHandler::Async(handler) => handler(args).await,
                };
                self.finish_call(dispatch, outcome)
            }
        }
    }

    fn prepare_registered_tool(
        &mut self,
        name: &str,
        version: Option<&str>,
        args: Value,
        options: CallOptions,
        allow_async: bool,
    ) -> Result<RegisteredPreparation> {
        canonical_bytes(&args)?;
        if !args.is_object() {
            return Err(Error::Invalid("tool args must be object".into()));
        }
        let registry = self
            .runtime
            .registry
            .clone()
            .ok_or_else(|| Error::Invalid("no registry attached".into()))?;
        let spec = match registry.get(name, None) {
            Ok(spec) => spec.clone(),
            Err(_) => {
                return self.refuse_policy(
                    format!(
                        "unknown registered action: {}",
                        version
                            .map(|v| format!("{name}@{v}"))
                            .unwrap_or_else(|| name.into())
                    ),
                    &json!({"tool":name,"args":args}),
                )
            }
        };
        let audit_args = spec.redact_args(&args)?;
        let blocked = json!({"tool":name,"args":audit_args});
        if version.is_some_and(|v| v != spec.version()) {
            return self.refuse_policy(
                format!(
                    "unknown registered action: {name}@{}",
                    version.unwrap_or_default()
                ),
                &blocked,
            );
        }
        if let Err(e) = spec.validate_args(&args) {
            let detail = match e {
                Error::Invalid(detail) => detail,
                other => other.to_string(),
            };
            return self.refuse_policy(format!("schema validation failed: {detail}"), &blocked);
        }
        let payload = json!({"tool":spec.name(),"version":spec.version(),"args":audit_args,"spec_digest":spec.spec_digest(),"registry_digest":registry.registry_digest()});
        if self.runtime.mode == ReplayMode::Replay {
            return match self.prepare_call(NodeKind::ToolCall, payload, options)? {
                CallPreparation::Recorded(node) => Ok(RegisteredPreparation::Ready(node)),
                CallPreparation::Dispatch(_) => unreachable!("strict replay never dispatches"),
            };
        }
        let candidate = Node::make(
            NodeKind::ToolCall,
            Some(&self.cursor_id),
            options.attempt,
            payload.clone(),
            None,
            json!({}),
        )?;
        if self.runtime.mode == ReplayMode::Record
            && self.runtime.refuse_duplicate_recordings
            && self.runtime.store.borrow().try_exists(&candidate.id)?
        {
            return Err(Error::DuplicateRecording(candidate.id));
        }
        for policy in self.runtime.policies.clone() {
            let ctx = PolicyContext {
                spec: spec.clone(),
                args: args.clone(),
                cursor_id: self.cursor_id.clone(),
                run_label: self.label.clone(),
                counters: self.report()?.spent,
            };
            match policy(&ctx) {
                Decision::Allow => {}
                Decision::Deny => return self.refuse_policy("denied by policy".into(), &payload),
                Decision::Confirm => {
                    self.pending.insert(
                        candidate.id.clone(),
                        Pending {
                            parent: self.cursor_id.clone(),
                            payload,
                            args,
                            spec,
                            options,
                        },
                    );
                    return Err(Error::ConfirmationRequired {
                        token: candidate.id,
                    });
                }
            }
        }
        if self.runtime.mode == ReplayMode::Hybrid
            && self.runtime.store.borrow().try_exists(&candidate.id)?
        {
            return match self.prepare_call(NodeKind::ToolCall, payload, options)? {
                CallPreparation::Recorded(node) => Ok(RegisteredPreparation::Ready(node)),
                CallPreparation::Dispatch(_) => unreachable!("existing hybrid recording"),
            };
        }
        if self.runtime.dry_run && spec.side_effects() {
            let estimates = self.precheck(NodeKind::ToolCall, &payload, options)?;
            let reservation = self.reserve_dispatch(NodeKind::ToolCall, &payload, &estimates)?;
            let node = Node::make(
                NodeKind::ToolCall,
                Some(&self.cursor_id),
                options.attempt,
                payload,
                None,
                json!({"dry_run":true,"charges":{"steps":1}}),
            )?;
            if let Some(reservation) = &reservation {
                self.runtime.store.0.inner.borrow_mut().settle_budget(
                    &reservation.id,
                    &BTreeMap::from([("steps".into(), Decimal::ONE)]),
                )?;
            }
            let node = self.runtime.structural(node)?;
            self.cursor_id = node.id.clone();
            return Ok(RegisteredPreparation::Ready(node));
        }
        let handler = if allow_async {
            spec.async_handler()
                .map(PreparedHandler::Async)
                .or_else(|| spec.handler().map(PreparedHandler::Sync))
        } else {
            spec.handler().map(PreparedHandler::Sync)
        };
        let Some(handler) = handler else {
            return self.refuse_policy("registered action has no handler".into(), &payload);
        };
        match self.prepare_call(NodeKind::ToolCall, payload, options)? {
            CallPreparation::Recorded(node) => Ok(RegisteredPreparation::Ready(node)),
            CallPreparation::Dispatch(dispatch) => Ok(RegisteredPreparation::Dispatch {
                dispatch,
                args,
                handler,
            }),
        }
    }

    /// Consume a policy-issued token exactly once at its original cursor.
    pub fn confirm(&mut self, token: &str) -> Result<Node> {
        let _guard = self.runtime.store.lock()?;
        let pending = self
            .pending
            .remove(token)
            .ok_or_else(|| Error::NotFound(token.into()))?;
        if self.cursor_id != pending.parent {
            return Err(Error::Invalid("cannot confirm after cursor moved".into()));
        }
        let Some(handler) = pending.spec.handler() else {
            return self.refuse_policy("registered action has no handler".into(), &pending.payload);
        };
        self.call(
            NodeKind::ToolCall,
            pending.payload,
            pending.args,
            pending.options,
            move |args| handler(args),
        )
    }

    pub async fn confirm_async(&mut self, token: &str) -> Result<Node> {
        let _guard = self.operation_lock()?;
        let pending = self
            .pending
            .remove(token)
            .ok_or_else(|| Error::NotFound(token.into()))?;
        if self.cursor_id != pending.parent {
            return Err(Error::Invalid("cannot confirm after cursor moved".into()));
        }
        let handler = pending
            .spec
            .async_handler()
            .map(PreparedHandler::Async)
            .or_else(|| pending.spec.handler().map(PreparedHandler::Sync));
        let Some(handler) = handler else {
            return self.refuse_policy("registered action has no handler".into(), &pending.payload);
        };
        match self.prepare_call(NodeKind::ToolCall, pending.payload, pending.options)? {
            CallPreparation::Recorded(node) => Ok(node),
            CallPreparation::Dispatch(dispatch) => {
                let outcome = match handler {
                    PreparedHandler::Sync(handler) => handler(pending.args),
                    PreparedHandler::Async(handler) => handler(pending.args).await,
                };
                self.finish_call(dispatch, outcome)
            }
        }
    }

    pub fn note(&mut self, payload: Value, attempt: u64) -> Result<Node> {
        let _guard = self.runtime.store.lock()?;
        self.note_inner(payload, attempt)
    }
    fn note_inner(&mut self, payload: Value, attempt: u64) -> Result<Node> {
        let candidate = Node::make(
            NodeKind::Note,
            Some(&self.cursor_id),
            attempt,
            payload.clone(),
            None,
            json!({}),
        )?;
        if self.runtime.mode != ReplayMode::Replay {
            self.precheck(NodeKind::Note, &payload, CallOptions::default())?;
        }
        let node = self.runtime.structural(candidate)?;
        self.cursor_id = node.id.clone();
        Ok(node)
    }

    /// Branch shares stored charges and budget scopes. Adopt explicitly to move
    /// the parent's cursor; abandoning a branch never refunds dispatched work.
    pub fn branch(&mut self, attempt: u64, budget: Option<Budget>) -> Result<Self> {
        let _guard = self.runtime.store.lock()?;
        if let Some(b) = &budget {
            b.validate()?;
        }
        let old_cursor = self.cursor_id.clone();
        let anchor = self.note_inner(json!({"branch":true}), attempt)?;
        self.cursor_id = old_cursor;
        let mut scopes = self.scopes.clone();
        if let Some(budget) = budget {
            scopes.push(Scope {
                budget,
                anchor: anchor.id.clone(),
            });
        }
        Ok(Self {
            runtime: self.runtime.clone(),
            root_id: self.root_id.clone(),
            cursor_id: anchor.id,
            label: self.label.clone(),
            scopes,
            pending: BTreeMap::new(),
            avoided: Charges::default(),
            avoided_meters: BTreeMap::new(),
            meter_scopes: self.meter_scopes.clone(),
        })
    }
    pub fn branch_with_meters(
        &mut self,
        attempt: u64,
        budget: Option<Budget>,
        meter_budget: MeterBudget,
    ) -> Result<Self> {
        validate_amounts(&meter_budget)?;
        let mut branch = self.branch(attempt, budget)?;
        branch
            .meter_scopes
            .push((branch.cursor_id.clone(), meter_budget));
        Ok(branch)
    }
    pub fn adopt(&mut self, branch: &Self) -> Result<Node> {
        let _guard = self.runtime.store.lock()?;
        if !Rc::ptr_eq(&self.runtime.store.0, &branch.runtime.store.0)
            || self.root_id != branch.root_id
        {
            return Err(Error::Invalid(
                "branch belongs to another run or store".into(),
            ));
        }
        if !self.is_ancestor(&self.cursor_id, &branch.cursor_id)? {
            return Err(Error::Invalid(
                "branch does not descend from current cursor".into(),
            ));
        }
        self.cursor_id = branch.cursor_id.clone();
        self.cursor()
    }
    pub fn rollback(&mut self, target: &str) -> Result<Node> {
        let _guard = self.runtime.store.lock()?;
        if !self.is_ancestor(target, &self.cursor_id)? {
            return Err(Error::Invalid("rollback target must be an ancestor".into()));
        }
        self.cursor_id = target.into();
        self.cursor()
    }

    fn is_ancestor(&self, target: &str, from: &str) -> Result<bool> {
        let mut current = Some(from.to_owned());
        let mut seen = BTreeSet::new();
        while let Some(id) = current {
            if !seen.insert(id.clone()) {
                return Err(Error::Integrity("cycle in ancestry".into()));
            }
            if id == target {
                return Ok(true);
            }
            current = self.runtime.store.borrow().get(&id)?.parent;
        }
        Ok(false)
    }

    fn accounting(&self, anchor: &str) -> Result<(Charges, bool)> {
        let total = self.meter_accounting(anchor)?;
        Ok((
            charges_from_meta(&json!({"charges":amounts_json(&total)}))?,
            false,
        ))
    }

    fn meter_accounting(&self, anchor: &str) -> Result<MeterCharges> {
        for _ in 0..3 {
            let trusted = self.runtime.store.synchronize_index();
            if trusted {
                if let Some(total) = self.runtime.store.0.index.borrow().totals.get(anchor) {
                    return Ok(total.clone());
                }
                self.runtime.verified(anchor)?;
            }
            let revision = self.runtime.store.0.index.borrow().revision;
            let nodes = self.runtime.store.borrow().walk(anchor)?;
            let mut total = MeterCharges::new();
            for node in &nodes {
                node.validate()?;
                add_amounts(&mut total, &meter_charges_from_meta(&node.meta)?)?;
            }
            if trusted {
                if self.runtime.store.borrow().cache_revision() != revision {
                    self.runtime.store.synchronize_index();
                    continue;
                }
                let mut index = self.runtime.store.0.index.borrow_mut();
                for node in &nodes {
                    index.remember(node)?;
                }
                index.totals.insert(anchor.into(), total.clone());
            }
            return Ok(total);
        }
        Err(Error::Integrity(
            "recording changed repeatedly during accounting".into(),
        ))
    }

    fn depth(&self) -> Result<u64> {
        self.depth_at(&self.cursor_id)
    }
    fn depth_at(&self, node_id: &str) -> Result<u64> {
        if self.runtime.store.synchronize_index() {
            self.runtime.verified(node_id)?;
            if let Some((_, _, depth)) = self.runtime.store.0.index.borrow().ancestry.get(node_id) {
                return Ok(*depth);
            }
        }

        let mut current = Some(node_id.to_owned());
        let mut count = 0u64;
        let mut seen = BTreeSet::new();
        while let Some(id) = current {
            if !seen.insert(id.clone()) {
                return Err(Error::Integrity("cycle in ancestry".into()));
            }
            current = self.runtime.store.borrow().get(&id)?.parent;
            if current.is_some() {
                count = count
                    .checked_add(1)
                    .ok_or_else(|| Error::Integrity("depth overflow".into()))?;
            }
        }
        Ok(count)
    }

    fn precheck(
        &mut self,
        kind: NodeKind,
        payload: &Value,
        options: CallOptions,
    ) -> Result<MeterCharges> {
        self.runtime.verified(&self.cursor_id)?;
        let call = matches!(kind, NodeKind::ModelCall | NodeKind::ToolCall);
        let mut estimates = MeterCharges::new();
        if call && self.runtime.core_meters {
            estimates.insert("steps".into(), 1.0);
        }
        for meter in self.runtime.meters.clone() {
            let estimate = match meter.estimate(kind, payload) {
                Ok(estimate) => estimate,
                Err(Error::MeterPrecheckRefusal(refusal)) => {
                    refusal.validate()?;
                    let mut audit = json!({"reason":refusal.reason,"meter":meter.name(),"blocked_kind":kind.as_str(),
                        "blocked_payload_digest":digest_payload(payload)?,"detail":refusal.detail});
                    if let Some(requested) = &refusal.requested {
                        audit["requested"] = json!(requested);
                    }
                    if let Some(remaining) = &refusal.remaining {
                        audit["remaining"] = json!(remaining);
                    }
                    let node = self.runtime.structural(Node::make(
                        NodeKind::Refusal,
                        Some(&self.cursor_id),
                        0,
                        audit,
                        None,
                        refusal.audit_meta.clone(),
                    )?)?;
                    self.cursor_id = node.id.clone();
                    return Err(Error::BudgetExceeded {
                        meter: meter.name().into(),
                        node_id: node.id,
                    });
                }
                Err(error) => return Err(error),
            };
            if let Some(amount) = estimate {
                validate_amount(amount, meter.name())?;
                estimates.insert(meter.name().into(), amount);
            }
        }
        if call {
            if let Some(tokens) = options.estimated_tokens {
                safe_amount(tokens, "estimated tokens")?;
                estimates.insert("tokens".into(), tokens as f64);
            }
        }
        let next_depth = if self.scopes.iter().any(|scope| scope.budget.depth.is_some())
            || self
                .meter_scopes
                .iter()
                .any(|(_, budget)| budget.contains_key("depth"))
        {
            self.depth()?
                .checked_add(1)
                .ok_or_else(|| Error::Integrity("depth overflow".into()))?
        } else {
            0
        };
        for scope in self.scopes.clone() {
            let spent = self.meter_accounting(&scope.anchor)?;
            for (meter, limit, already, requested) in [
                (
                    "steps",
                    scope.budget.steps,
                    *spent.get("steps").unwrap_or(&0.0),
                    *estimates.get("steps").unwrap_or(&0.0),
                ),
                (
                    "tokens",
                    scope.budget.tokens,
                    *spent.get("tokens").unwrap_or(&0.0),
                    *estimates.get("tokens").unwrap_or(&0.0),
                ),
                ("depth", scope.budget.depth, 0.0, next_depth as f64),
            ] {
                if let Some(limit) = limit {
                    if exceeds_limit(limit as f64, already, requested)? {
                        return self.refuse_budget(
                            kind,
                            payload,
                            meter,
                            requested,
                            decimal_subtract(limit as f64, already)?,
                            meter == "tokens" && call,
                            "budget",
                        );
                    }
                }
            }
        }
        for (anchor, budget) in self.meter_scopes.clone() {
            let spent = self.meter_accounting(&anchor)?;
            for (meter, limit) in budget {
                let already = if meter == "depth" {
                    0.0
                } else {
                    *spent.get(&meter).unwrap_or(&0.0)
                };
                let requested = if meter == "depth" {
                    next_depth as f64
                } else {
                    *estimates.get(&meter).unwrap_or(&0.0)
                };
                if exceeds_limit(limit, already, requested)? {
                    return self.refuse_budget(
                        kind,
                        payload,
                        &meter,
                        requested,
                        decimal_subtract(limit, already)?,
                        meter != "steps" && meter != "depth",
                        "budget",
                    );
                }
            }
        }
        for meter in self.runtime.meters.clone() {
            if let Some(window) = meter.window() {
                let ledger_key = meter
                    .window_ledger_key(&self.root_id)?
                    .ok_or_else(|| Error::Invalid("window meter requires a ledger key".into()))?;
                let cutoff = unix_seconds() - window.seconds;
                let mut spent = 0.0;
                for node in self.runtime.store.borrow().walk(&self.root_id)? {
                    node.validate()?;
                    if let Some(events) = node.meta.get("window_events").and_then(Value::as_array) {
                        for event in events {
                            if event
                                .get("keys")
                                .and_then(|keys| keys.get(meter.name()))
                                .and_then(Value::as_str)
                                == Some(ledger_key.as_str())
                                && event
                                    .get("time")
                                    .and_then(Value::as_f64)
                                    .is_some_and(|time| time > cutoff)
                            {
                                spent = decimal_add(
                                    spent,
                                    meter_charges_from_meta(event)?
                                        .get(meter.name())
                                        .copied()
                                        .unwrap_or(0.0),
                                )?;
                            }
                        }
                    } else if node
                        .meta
                        .get("window_keys")
                        .and_then(|keys| keys.get(meter.name()))
                        .and_then(Value::as_str)
                        == Some(ledger_key.as_str())
                        && node
                            .meta
                            .get("created_at_unix")
                            .and_then(Value::as_f64)
                            .is_some_and(|time| time > cutoff)
                    {
                        spent = decimal_add(
                            spent,
                            meter_charges_from_meta(&node.meta)?
                                .get(meter.name())
                                .copied()
                                .unwrap_or(0.0),
                        )?;
                    }
                }
                let requested = estimates.get(meter.name()).copied().unwrap_or(0.0);
                if exceeds_limit(window.limit, spent, requested)? {
                    let payload = json!({"reason":"window","meter":meter.name(),"blocked_kind":kind.as_str(),
                        "blocked_payload_digest":digest_payload(payload)?,"requested":requested.to_string(),
                        "remaining":decimal_subtract(window.limit,spent)?.to_string(), "window_seconds":if window.seconds.fract()==0.0 { json!(window.seconds as u64) } else { json!(window.seconds.to_string()) }});
                    let refusal = self.runtime.structural(Node::make(
                        NodeKind::Refusal,
                        Some(&self.cursor_id),
                        0,
                        payload,
                        None,
                        json!({}),
                    )?)?;
                    self.cursor_id = refusal.id.clone();
                    return Err(Error::BudgetExceeded {
                        meter: meter.name().into(),
                        node_id: refusal.id,
                    });
                }
            }
        }
        Ok(estimates)
    }

    #[allow(clippy::too_many_arguments)]
    fn refuse_budget<T>(
        &mut self,
        kind: NodeKind,
        payload: &Value,
        meter: &str,
        requested: f64,
        remaining: f64,
        estimated: bool,
        detail: &str,
    ) -> Result<T> {
        let mut audit = json!({"reason":"budget","meter":meter,"blocked_kind":kind.as_str(),"blocked_payload_digest":digest_payload(payload)?,"requested":requested.to_string(),"remaining":remaining.to_string()});
        if estimated {
            audit["estimated"] = json!("true");
        }
        if detail != "budget" {
            audit["detail"] = json!(detail);
        }
        let candidate = Node::make(
            NodeKind::Refusal,
            Some(&self.cursor_id),
            0,
            audit,
            None,
            json!({}),
        )?;
        let node = self.runtime.structural(candidate)?;
        self.cursor_id = node.id.clone();
        Err(Error::BudgetExceeded {
            meter: meter.into(),
            node_id: node.id,
        })
    }

    fn refuse_policy<T>(&mut self, detail: String, payload: &Value) -> Result<T> {
        let mut audit = json!({"reason":"policy","detail":detail,"blocked_kind":"tool_call","blocked_payload_digest":digest_payload(payload)?});
        if let Some(registry) = &self.runtime.registry {
            audit["registry_digest"] = json!(registry.registry_digest());
        }
        let candidate = Node::make(
            NodeKind::Refusal,
            Some(&self.cursor_id),
            0,
            audit,
            None,
            json!({}),
        )?;
        let node = self.runtime.structural(candidate)?;
        self.cursor_id = node.id.clone();
        Err(Error::PolicyViolation {
            detail,
            node_id: node.id,
        })
    }

    fn call<F>(
        &mut self,
        kind: NodeKind,
        payload: Value,
        handler_args: Value,
        options: CallOptions,
        handler: F,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<Value>,
    {
        match self.prepare_call(kind, payload, options)? {
            CallPreparation::Recorded(node) => Ok(node),
            CallPreparation::Dispatch(dispatch) => {
                let outcome = handler(handler_args);
                self.finish_call(dispatch, outcome)
            }
        }
    }

    fn reserve_dispatch(
        &mut self,
        kind: NodeKind,
        payload: &Value,
        estimates: &MeterCharges,
    ) -> Result<Option<DispatchReservation>> {
        if !self.runtime.store.borrow().supports_reservations() {
            return Ok(None);
        }
        let mut limits: BTreeMap<String, MeterBudget> = BTreeMap::new();
        for scope in &self.scopes {
            let entry = limits.entry(scope.anchor.clone()).or_default();
            for (name, limit) in [
                ("steps", scope.budget.steps),
                ("tokens", scope.budget.tokens),
            ] {
                if let Some(limit) = limit {
                    entry.insert(name.into(), limit as f64);
                }
            }
        }
        for (anchor, extra) in &self.meter_scopes {
            let entry = limits.entry(anchor.clone()).or_default();
            for (name, limit) in extra {
                if name != "depth" {
                    entry
                        .entry(name.clone())
                        .and_modify(|old| *old = old.min(*limit))
                        .or_insert(*limit);
                }
            }
        }
        let mut budgets = Vec::new();
        for (anchor, limits) in limits {
            if limits.is_empty() {
                continue;
            }
            let mut baseline = MeterCharges::new();
            for node in self.runtime.store.borrow().walk(&anchor)? {
                node.validate()?;
                // Outstanding reservations are included atomically by the arbiter.
                if node.meta.get("state").and_then(Value::as_str) == Some("pending")
                    && node.meta.get("reservation_id").is_some()
                {
                    continue;
                }
                add_amounts(&mut baseline, &meter_charges_from_meta(&node.meta)?)?;
            }
            budgets.push(crate::BudgetReservation {
                scope_id: anchor,
                limits: decimal_amounts(&limits)?,
                baseline: decimal_amounts(&baseline)?,
                estimates: decimal_amounts(estimates)?,
            });
        }
        let mut windows = Vec::new();
        for meter in &self.runtime.meters {
            if let Some(window) = meter.window() {
                windows.push(crate::WindowReservation {
                    ledger_key: meter.window_ledger_key(&self.root_id)?.ok_or_else(|| {
                        Error::Invalid("window meter requires a ledger key".into())
                    })?,
                    meter: meter.name().into(),
                    limit: decimal_amount(window.limit)?,
                    amount: decimal_amount(estimates.get(meter.name()).copied().unwrap_or(0.0))?,
                    window_seconds: window.seconds,
                });
            }
        }
        if budgets.is_empty() && windows.is_empty() {
            return Ok(None);
        }
        static SERIAL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let serial = SERIAL.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let id = digest_payload(
            &json!({"root":self.root_id,"process":std::process::id(),"time":now.to_string(),"serial":serial.to_string()}),
        )?;
        let check = self.runtime.store.0.inner.borrow_mut().reserve_budget(
            &id,
            &budgets,
            &windows,
            self.runtime.reservation_lease_seconds,
        )?;
        let Some(check) = check else {
            return Ok(None);
        };
        if !check.ok {
            let meter = check.meter.unwrap_or_else(|| "unknown".into());
            let mut audit = json!({"reason":check.reason,"meter":meter,"blocked_kind":kind.as_str(),
                "blocked_payload_digest":digest_payload(payload)?,"requested":check.requested.normalize().to_string(),
                "remaining":check.remaining.normalize().to_string()});
            if let Some(seconds) = check.window_seconds {
                audit["window_seconds"] = if seconds.fract() == 0.0 {
                    json!(seconds as u64)
                } else {
                    json!(seconds.to_string())
                };
            }
            let node = self.runtime.structural(Node::make(
                NodeKind::Refusal,
                Some(&self.cursor_id),
                0,
                audit,
                None,
                json!({}),
            )?)?;
            self.cursor_id = node.id.clone();
            return Err(Error::BudgetExceeded {
                meter,
                node_id: node.id,
            });
        }
        let heartbeat = self.runtime.store.borrow().lease_renewer().map(|renew| {
            LeaseHeartbeat::start(id.clone(), self.runtime.reservation_lease_seconds, renew)
        });
        Ok(Some(DispatchReservation {
            id,
            heartbeat,
            store: self.runtime.store.clone(),
            prepared: false,
        }))
    }

    pub(crate) fn prepare_call(
        &mut self,
        kind: NodeKind,
        payload: Value,
        options: CallOptions,
    ) -> Result<CallPreparation> {
        self.prepare_call_metered(kind, payload, options, None)
    }
    fn prepare_call_metered(
        &mut self,
        kind: NodeKind,
        payload: Value,
        options: CallOptions,
        meter_payload: Option<Value>,
    ) -> Result<CallPreparation> {
        let meter_payload = meter_payload.unwrap_or_else(|| payload.clone());
        let candidate = Node::make(
            kind,
            Some(&self.cursor_id),
            options.attempt,
            payload.clone(),
            None,
            json!({}),
        )?;
        let exists = self.runtime.store.borrow().try_exists(&candidate.id)?;
        if exists {
            let node = self.runtime.verified(&candidate.id)?;
            let incomplete = node.result_text.is_none()
                || node
                    .meta
                    .get("state")
                    .and_then(Value::as_str)
                    .is_some_and(|s| s != "completed");
            if self.runtime.mode == ReplayMode::Record {
                if self.runtime.refuse_duplicate_recordings || incomplete {
                    return Err(Error::DuplicateRecording(candidate.id));
                }
            } else {
                if incomplete {
                    return Err(if self.runtime.mode == ReplayMode::Replay {
                        Error::MissingRecording(node.id)
                    } else {
                        Error::DuplicateRecording(node.id)
                    });
                }
                let mut avoided = MeterCharges::new();
                if self.runtime.core_meters {
                    avoided.insert("steps".into(), 1.0);
                    if let Some(tokens) = node.result.as_ref().and_then(usage_tokens) {
                        if tokens != 0 {
                            avoided.insert("tokens".into(), tokens as f64);
                        }
                    }
                }
                let mut replay_meta = node.meta.clone();
                for meter in &self.runtime.meters {
                    let amount = meter.charge_with_meta(
                        kind,
                        &payload,
                        node.result.as_ref().expect("replayable result"),
                        &mut replay_meta,
                    )?;
                    validate_amount(amount, meter.name())?;
                    if amount != 0.0 {
                        avoided.insert(meter.name().into(), amount);
                    } else {
                        avoided.remove(meter.name());
                    }
                }
                self.avoided.add(charges_from_meta(
                    &json!({"charges":amounts_json(&avoided)}),
                )?)?;
                add_amounts(&mut self.avoided_meters, &avoided)?;
                if self.runtime.mode == ReplayMode::Hybrid {
                    let mut recorded_avoided = node
                        .meta
                        .get("avoided")
                        .cloned()
                        .unwrap_or_else(|| json!({}));
                    let mut total = meter_charges_from_meta(&json!({"charges":recorded_avoided}))?;
                    add_amounts(&mut total, &avoided)?;
                    recorded_avoided = amounts_json(&total);
                    self.runtime
                        .update_meta(&node.id, json!({"avoided":recorded_avoided}))?;
                }
                self.cursor_id = node.id.clone();
                return Ok(CallPreparation::Recorded(node));
            }
        }
        if self.runtime.mode == ReplayMode::Replay {
            return Err(Error::MissingRecording(candidate.id));
        }
        let estimates = self.precheck(kind, &meter_payload, options)?;
        let mut reservation = self.reserve_dispatch(kind, &meter_payload, &estimates)?;
        let mut pending = Node::make(
            kind,
            Some(&self.cursor_id),
            options.attempt,
            payload,
            None,
            json!({"state":"pending","charges":amounts_json(&estimates),
                "accounting_unknown":true,"created_at_unix":unix_seconds()}),
        )?;
        let mut window_keys = serde_json::Map::new();
        for meter in &self.runtime.meters {
            if let Some(key) = meter.window_ledger_key(&self.root_id)? {
                window_keys.insert(meter.name().into(), json!(key));
            }
        }
        if !window_keys.is_empty() {
            pending.meta["window_keys"] = Value::Object(window_keys);
        }
        if let Some(reservation) = &reservation {
            pending.meta["reservation_id"] = json!(reservation.id);
        }
        let started = Instant::now();
        // Establish every context before staging or invoking a callback. A
        // failed setup releases the reservation and leaves the cursor intact.
        let measurements =
            Measurements::start(&self.runtime.meters, self.runtime.cleanup_errors.clone())?;
        if !exists {
            // The unprepared reservation guard releases once on staging error.
            self.runtime.write_node(pending.clone(), false)?;
            self.cursor_id = pending.id.clone();
        }
        if let Some(reservation) = &mut reservation {
            reservation.prepared = true;
        }
        Ok(CallPreparation::Dispatch(PendingDispatch {
            pending,
            estimates,
            duplicate: exists,
            started,
            measurements,
            reservation,
            meter_payload,
            store: self.runtime.store.clone(),
            armed: true,
            token_estimate_override: options.estimated_tokens.is_some(),
        }))
    }

    pub(crate) fn finish_call(
        &mut self,
        dispatch: PendingDispatch,
        outcome: Result<Value>,
    ) -> Result<Node> {
        let completed_or_unknown = match &outcome {
            Ok(result) => result.is_object(),
            Err(error) => error.is_post_dispatch_outcome_unknown(),
        };
        // Every failure after a usable provider result must preserve retry
        // classification, including serialization, settlement and store I/O.
        self.finish_call_inner(dispatch, outcome).map_err(|error| {
            if completed_or_unknown && !error.is_post_dispatch_outcome_unknown() {
                Error::OutcomeUnknown(Box::new(error))
            } else {
                error
            }
        })
    }

    fn finish_call_inner(
        &mut self,
        mut dispatch: PendingDispatch,
        outcome: Result<Value>,
    ) -> Result<Node> {
        // Python's duration includes context setup, but excludes context exit.
        let duration = dispatch.started.elapsed().as_secs_f64();
        let outcome = outcome.and_then(|result| {
            if result.is_object() {
                Ok(result)
            } else {
                Err(Error::Handler("callback must return a JSON object".into()))
            }
        });
        let cleanup_error = dispatch.measurements.stop(outcome.as_ref().err());
        let lease_lost = dispatch
            .reservation
            .as_mut()
            .and_then(|reservation| reservation.heartbeat.as_mut())
            .and_then(LeaseHeartbeat::stop);
        let result = match outcome {
            Ok(result) => result,
            Err(Error::OutcomeUnknown(error)) => {
                self.fail_pending(&dispatch, "outcome_unknown")?;
                dispatch.armed = false;
                return Err(Error::OutcomeUnknown(error));
            }
            Err(error) => {
                self.discard_pending(&dispatch);
                dispatch.armed = false;
                return Err(error);
            }
        };
        if let Some(error) = cleanup_error {
            self.fail_pending(&dispatch, "measurement_cleanup_error")?;
            dispatch.armed = false;
            return Err(Error::OutcomeUnknown(Box::new(error)));
        }
        let mut meta = json!({"state":"completed","duration_s":duration,
            "created_at_unix":unix_seconds()});
        match dispatch.measurements.readings() {
            Ok(readings) => {
                meta.as_object_mut()
                    .expect("runtime metadata")
                    .extend(readings);
            }
            Err(error) => {
                self.fail_pending(&dispatch, "measurement_readings_error")?;
                dispatch.armed = false;
                return Err(Error::OutcomeUnknown(Box::new(error)));
            }
        }
        let mut charges = MeterCharges::new();
        if self.runtime.core_meters {
            charges.insert("steps".into(), 1.0);
        }
        let actual = usage_tokens(&result);
        if self.runtime.core_meters {
            if let Some(tokens) = actual {
                charges.insert("tokens".into(), tokens as f64);
            } else if let Some(estimate) = dispatch.estimates.get("tokens") {
                charges.insert("tokens".into(), *estimate);
                meta["accounting_fallbacks"] = json!({"tokens":{
                "reason":"missing_or_invalid_provider_usage","source":"precheck_estimate"}});
            }
        }
        if let Some(usage) = result.get("usage").filter(|v| v.is_object()) {
            meta["usage"] = usage.clone();
        }
        for meter in self.runtime.meters.clone() {
            let charged = meter.charge_with_meta(
                dispatch.pending.kind,
                &dispatch.meter_payload,
                &result,
                &mut meta,
            );
            let mut amount = match charged.and_then(|amount| {
                validate_amount(amount, meter.name())?;
                Ok(amount)
            }) {
                Ok(amount) => amount,
                Err(error) => {
                    self.fail_pending(&dispatch, "meter_error")?;
                    dispatch.armed = false;
                    return Err(error);
                }
            };
            if amount == 0.0
                && (meter.precheck_is_estimate()
                    || (meter.name() == "tokens" && dispatch.token_estimate_override))
            {
                let reason = meter
                    .precheck_fallback_reason(
                        dispatch.pending.kind,
                        &dispatch.meter_payload,
                        &result,
                        &meta,
                    )
                    .or_else(|| {
                        actual
                            .is_none()
                            .then(|| "missing_or_invalid_provider_usage".into())
                    });
                if let (Some(estimate), Some(reason)) =
                    (dispatch.estimates.get(meter.name()), reason)
                {
                    amount = *estimate;
                    if !meta["accounting_fallbacks"].is_object() {
                        meta["accounting_fallbacks"] = json!({});
                    }
                    meta["accounting_fallbacks"][meter.name()] = json!({
                        "reason":reason,"source":"precheck_estimate"});
                }
            }
            charges.insert(meter.name().into(), amount);
        }
        meta["charges"] = amounts_json(&charges);
        if let Some(detail) = &lease_lost {
            meta["reservation_lease"] = json!({"status":"lost","detail":detail});
        }
        let window_charges: MeterCharges = self
            .runtime
            .meters
            .iter()
            .filter(|meter| meter.window().is_some())
            .filter_map(|meter| {
                charges
                    .get(meter.name())
                    .map(|amount| (meter.name().into(), *amount))
            })
            .collect();
        let window_event = json!({"time":unix_seconds(),"charges":amounts_json(&window_charges),"keys":dispatch.pending.meta.get("window_keys")});
        if !window_charges.is_empty() {
            meta["window_events"] = json!([window_event]);
        }
        let node = Node::make(
            dispatch.pending.kind,
            dispatch.pending.parent.as_deref(),
            dispatch.pending.attempt,
            dispatch.pending.payload.clone(),
            Some(result),
            meta,
        )?;
        if let Some(reservation) = &dispatch.reservation {
            self.runtime
                .store
                .0
                .inner
                .borrow_mut()
                .settle_budget(&reservation.id, &decimal_amounts(&charges)?)?;
        }
        if dispatch.duplicate {
            self.runtime.write_node(node.clone(), false)?;
            if !window_charges.is_empty() {
                let stored = self.runtime.store.borrow().get(&node.id)?;
                let mut events = stored
                    .meta
                    .get("window_events")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                if events.is_empty() {
                    events.push(json!({"time":stored.meta.get("created_at_unix"),"charges":stored.meta.get("charges"),"keys":stored.meta.get("window_keys")}));
                }
                events.push(window_event);
                self.runtime
                    .update_meta(&node.id, json!({"window_events":events}))?;
            }
        } else {
            self.runtime.write_node(node.clone(), true)?;
            self.runtime.notify_node(&node);
        }
        let stored = self.runtime.store.borrow().get(&node.id)?;
        self.cursor_id = stored.id.clone();
        dispatch.armed = false;
        if lease_lost.is_some() {
            return Err(Error::Integrity(format!(
                "reservation lease lost while call was running: {}",
                stored.id
            )));
        }
        Ok(stored)
    }

    fn discard_pending(&mut self, dispatch: &PendingDispatch) {
        if let Some(reservation) = &dispatch.reservation {
            let _ = self
                .runtime
                .store
                .0
                .inner
                .borrow_mut()
                .release_budget(&reservation.id);
        }
        if dispatch.duplicate {
            return;
        }
        let ids = BTreeSet::from([dispatch.pending.id.clone()]);
        let removed = self.runtime.store.0.inner.borrow_mut().drop_nodes(&ids);
        if removed.is_ok() {
            self.runtime.store.synchronize_index();
            if let Some(parent) = &dispatch.pending.parent {
                self.cursor_id = parent.clone();
            }
        } else {
            // Backends without removal support retain a durable conservative record.
            let _ = self.fail_pending(dispatch, "handler_error_cleanup_failed");
        }
    }

    fn fail_pending(&self, dispatch: &PendingDispatch, reason: &str) -> Result<()> {
        if let Some(reservation) = &dispatch.reservation {
            self.runtime
                .store
                .0
                .inner
                .borrow_mut()
                .settle_budget(&reservation.id, &decimal_amounts(&dispatch.estimates)?)?;
        }
        if dispatch.duplicate {
            let stored = self.runtime.store.borrow().get(&dispatch.pending.id)?;
            let mut events = stored
                .meta
                .get("window_events")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if events.is_empty() {
                events.push(json!({"time":stored.meta.get("created_at_unix"),"charges":stored.meta.get("charges"),"keys":stored.meta.get("window_keys")}));
            }
            events.push(json!({"time":unix_seconds(),"charges":amounts_json(&dispatch.estimates),"keys":dispatch.pending.meta.get("window_keys")}));
            self.runtime
                .update_meta(&stored.id, json!({"window_events":events}))?;
            return Ok(());
        }
        let pending = &dispatch.pending;
        let failed = Node::make(
            pending.kind,
            pending.parent.as_deref(),
            pending.attempt,
            pending.payload.clone(),
            None,
            json!({"state":"failed",
                "charges":amounts_json(&dispatch.estimates),"accounting_unknown":true,"error":reason,"created_at_unix":unix_seconds(),"window_keys":pending.meta.get("window_keys")}),
        )?;
        self.runtime.write_node(failed.clone(), true)?;
        self.runtime.notify_node(&failed);
        Ok(())
    }
}

enum PreparedHandler {
    Sync(crate::Handler),
    Async(crate::AsyncHandler),
}
// Dispatch is held briefly on the stack; avoid a heap allocation on every call.
#[allow(clippy::large_enum_variant)]
enum RegisteredPreparation {
    Ready(Node),
    Dispatch {
        dispatch: PendingDispatch,
        args: Value,
        handler: PreparedHandler,
    },
}
struct PreparedRevalidation {
    recorded: Node,
    recorded_contract: Option<Value>,
    live_contract: Value,
    live_payload: Value,
    observation_payload: Value,
    options: RevalidationOptions,
}

// Keep the staged callback path allocation-free beyond the owned JSON values.
#[allow(clippy::large_enum_variant)]
pub(crate) enum CallPreparation {
    Recorded(Node),
    Dispatch(PendingDispatch),
}

struct Measurements {
    contexts: Vec<Box<dyn crate::MeterMeasurement>>,
    stopped: bool,
    errors: Rc<RefCell<Vec<String>>>,
}
impl Measurements {
    fn start(meters: &[Rc<dyn crate::Meter>], errors: Rc<RefCell<Vec<String>>>) -> Result<Self> {
        let mut active = Self {
            contexts: Vec::new(),
            stopped: false,
            errors,
        };
        for meter in meters {
            let setup = meter.measure().and_then(|context| {
                if let Some(mut context) = context {
                    context.start()?;
                    active.contexts.push(context);
                }
                Ok(())
            });
            if let Err(error) = setup {
                active.stop(Some(&error));
                return Err(error);
            }
        }
        Ok(active)
    }
    fn stop(&mut self, error: Option<&Error>) -> Option<Error> {
        if self.stopped {
            return None;
        }
        self.stopped = true;
        let mut first_error = None;
        for context in self.contexts.iter_mut().rev() {
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| context.finish(error)))
                    .unwrap_or_else(|_| Err(Error::Handler("measurement cleanup panicked".into())));
            if let Err(error) = result {
                self.errors.borrow_mut().push(error.to_string());
                if first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }
        first_error
    }
    fn readings(&self) -> Result<serde_json::Map<String, Value>> {
        let mut readings = serde_json::Map::new();
        for context in &self.contexts {
            let value = context.readings()?;
            let object = value.as_object().ok_or_else(|| {
                Error::Invalid("measurement readings must be a JSON object".into())
            })?;
            readings.extend(object.clone());
        }
        Ok(readings)
    }
}
impl Drop for Measurements {
    fn drop(&mut self) {
        self.stop(Some(&Error::OutcomeUnknown(Box::new(Error::Handler(
            "measurement scope cancelled or unwound".into(),
        )))));
    }
}

pub(crate) struct PendingDispatch {
    pending: Node,
    meter_payload: Value,
    estimates: MeterCharges,
    duplicate: bool,
    started: Instant,
    measurements: Measurements,
    reservation: Option<DispatchReservation>,
    store: SharedStore,
    armed: bool,
    token_estimate_override: bool,
}
impl Drop for PendingDispatch {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.measurements
            .stop(Some(&Error::OutcomeUnknown(Box::new(Error::Handler(
                "call cancelled or unwound".into(),
            )))));
        if let Some(heartbeat) = self
            .reservation
            .as_mut()
            .and_then(|reservation| reservation.heartbeat.as_mut())
        {
            heartbeat.stop();
        }
        let Ok(mut store) = self.store.0.inner.try_borrow_mut() else {
            return;
        };
        if let Some(reservation) = &self.reservation {
            if let Ok(charges) = decimal_amounts(&self.estimates) {
                let _ = store.settle_budget(&reservation.id, &charges);
            }
        }
        if self.duplicate {
            if self.pending.meta.get("window_keys").is_some() {
                if let Ok(stored) = store.get(&self.pending.id) {
                    let mut events = stored
                        .meta
                        .get("window_events")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    if events.is_empty() {
                        events.push(json!({"time":stored.meta.get("created_at_unix"),"charges":stored.meta.get("charges"),"keys":stored.meta.get("window_keys")}));
                    }
                    events.push(json!({"time":unix_seconds(),"charges":amounts_json(&self.estimates),"keys":self.pending.meta.get("window_keys")}));
                    let _ = store.update_meta(&stored.id, json!({"window_events":events}));
                }
            }
            return;
        }
        if let Ok(failed) = Node::make(
            self.pending.kind,
            self.pending.parent.as_deref(),
            self.pending.attempt,
            self.pending.payload.clone(),
            None,
            json!({"state":"failed","charges":amounts_json(&self.estimates),
                "accounting_unknown":true,"error":"outcome_unknown","created_at_unix":unix_seconds(),"window_keys":self.pending.meta.get("window_keys")}),
        ) {
            let _ = store.finalize(failed);
        }
    }
}
struct DispatchReservation {
    id: String,
    heartbeat: Option<LeaseHeartbeat>,
    store: SharedStore,
    prepared: bool,
}
impl Drop for DispatchReservation {
    fn drop(&mut self) {
        if !self.prepared {
            if let Ok(mut store) = self.store.0.inner.try_borrow_mut() {
                let _ = store.release_budget(&self.id);
            }
        }
    }
}
struct LeaseHeartbeat {
    shutdown: Option<std::sync::mpsc::Sender<()>>,
    lost: std::sync::Arc<std::sync::Mutex<Option<String>>>,
    finished: std::sync::mpsc::Receiver<()>,
}
impl LeaseHeartbeat {
    fn start(id: String, seconds: f64, renew: crate::store::LeaseRenewer) -> Self {
        let (send, receive) = std::sync::mpsc::channel();
        let (complete, finished) = std::sync::mpsc::channel();
        let lost = std::sync::Arc::new(std::sync::Mutex::new(None));
        let failures = lost.clone();
        let interval = std::time::Duration::from_secs_f64((seconds / 3.0).clamp(0.001, 20.0));
        std::thread::spawn(move || {
            loop {
                match receive.recv_timeout(interval) {
                    Ok(()) | Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                        let detail =
                            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                                renew(&id, seconds)
                            })) {
                                Ok(Ok(true)) => continue,
                                Ok(Ok(false)) => "reservation expired or disappeared".to_owned(),
                                Ok(Err(error)) => error.to_string(),
                                Err(_) => "reservation renewal panicked".to_owned(),
                            };
                        if let Ok(mut failure) = failures.lock() {
                            *failure = Some(detail);
                        }
                        break;
                    }
                }
            }
            let _ = complete.send(());
        });
        Self {
            shutdown: Some(send),
            lost,
            finished,
        }
    }
    fn stop(&mut self) -> Option<String> {
        if let Some(send) = self.shutdown.take() {
            let _ = send.send(());
            if matches!(
                self.finished
                    .recv_timeout(std::time::Duration::from_millis(100)),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout)
            ) {
                if let Ok(mut lost) = self.lost.lock() {
                    *lost = Some("reservation renewal did not stop promptly".into());
                }
            }
        }
        self.lost.lock().ok().and_then(|lost| lost.clone())
    }
}
impl Drop for LeaseHeartbeat {
    fn drop(&mut self) {
        self.stop();
    }
}
fn decimal_add(left: f64, right: f64) -> Result<f64> {
    crate::decimal::exact_add(decimal_amount(left)?, decimal_amount(right)?)
        .and_then(|amount| amount.to_f64())
        .ok_or_else(|| Error::Integrity("meter total overflow".into()))
}
fn decimal_subtract(left: f64, right: f64) -> Result<f64> {
    crate::decimal::exact_subtract(decimal_amount(left)?, decimal_amount(right)?)
        .and_then(|amount| amount.to_f64())
        .ok_or_else(|| Error::Integrity("meter total overflow".into()))
}
fn exceeds_limit(limit: f64, already: f64, requested: f64) -> Result<bool> {
    let remaining =
        crate::decimal::exact_subtract(decimal_amount(limit)?, decimal_amount(already)?)
            .ok_or_else(|| Error::Integrity("budget amount overflow".into()))?;
    Ok(decimal_amount(requested)? > remaining)
}
fn decimal_amount(amount: f64) -> Result<Decimal> {
    crate::parse_decimal_exact(&amount.to_string())
        .map_err(|error| Error::Invalid(format!("meter amount out of decimal range: {error}")))
}
fn decimal_amounts(amounts: &MeterCharges) -> Result<BTreeMap<String, Decimal>> {
    amounts
        .iter()
        .map(|(name, amount)| Ok((name.clone(), decimal_amount(*amount)?)))
        .collect()
}

fn validate_amount(amount: f64, name: &str) -> Result<()> {
    if !amount.is_finite() || amount < 0.0 {
        return Err(Error::Invalid(format!(
            "{name} amount must be finite and nonnegative"
        )));
    }
    decimal_amount(amount)?;
    Ok(())
}
fn validate_amounts(amounts: &MeterCharges) -> Result<()> {
    for (name, amount) in amounts {
        if name.trim().is_empty() {
            return Err(Error::Invalid("meter name cannot be empty".into()));
        }
        validate_amount(*amount, name)?;
    }
    Ok(())
}
fn unix_seconds() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn amounts_json(amounts: &MeterCharges) -> Value {
    let mut object = serde_json::Map::new();
    for (name, amount) in amounts {
        if *amount == 0.0 {
            continue;
        }
        let value = if amount.fract() == 0.0 && *amount <= MAX_SAFE_INTEGER as f64 {
            json!(*amount as u64)
        } else {
            json!(*amount)
        };
        object.insert(name.clone(), value);
    }
    Value::Object(object)
}
fn add_amounts(total: &mut MeterCharges, charges: &MeterCharges) -> Result<()> {
    for (name, amount) in charges {
        let next = decimal_add(total.get(name).copied().unwrap_or(0.0), *amount)?;
        validate_amount(next, name)?;
        if next != 0.0 {
            total.insert(name.clone(), next);
        }
    }
    Ok(())
}
fn meter_charges_from_meta(meta: &Value) -> Result<MeterCharges> {
    let Some(charges) = meta.get("charges") else {
        return Ok(MeterCharges::new());
    };
    let charges = charges
        .as_object()
        .ok_or_else(|| Error::Integrity("charges must be object".into()))?;
    let mut result = MeterCharges::new();
    for (name, amount) in charges {
        crate::parse_decimal_exact(&amount.to_string()).map_err(|error| {
            Error::Integrity(format!("{name} charge out of decimal range: {error}"))
        })?;
        let amount = amount
            .as_f64()
            .ok_or_else(|| Error::Integrity(format!("{name} charge must be numeric")))?;
        validate_amount(amount, name).map_err(|error| Error::Integrity(error.to_string()))?;
        if amount != 0.0 {
            result.insert(name.clone(), amount);
        }
    }
    Ok(result)
}

fn charges_from_meta(meta: &Value) -> Result<Charges> {
    let Some(charges) = meta.get("charges") else {
        return Ok(Charges::default());
    };
    if !charges.is_object() {
        return Err(Error::Integrity("charges must be object".into()));
    }
    let amount = |key: &str| -> Result<u64> {
        let Some(value) = charges.get(key) else {
            return Ok(0);
        };
        crate::parse_decimal_exact(&value.to_string()).map_err(|error| {
            Error::Integrity(format!("{key} charge out of decimal range: {error}"))
        })?;
        let amount = value
            .as_f64()
            .ok_or_else(|| Error::Integrity(format!("{key} charge must be nonnegative integer")))?;
        if !amount.is_finite()
            || amount < 0.0
            || amount.fract() != 0.0
            || amount > MAX_SAFE_INTEGER as f64
        {
            return Err(Error::Integrity(format!(
                "{key} charge must be nonnegative safe integer"
            )));
        }
        let n = amount as u64;
        safe_amount(n, key).map_err(|e| Error::Integrity(e.to_string()))?;
        Ok(n)
    };
    Ok(Charges {
        steps: amount("steps")?,
        tokens: amount("tokens")?,
    })
}
fn usage_tokens(result: &Value) -> Option<u64> {
    let usage = result.get("usage")?.as_object()?;
    let input = usage.get("input_tokens")?.as_u64()?;
    let output = usage.get("output_tokens")?.as_u64()?;
    let total = input.checked_add(output)?;
    if total > MAX_SAFE_INTEGER {
        None
    } else {
        Some(total)
    }
}
