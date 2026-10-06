use crate::identity::{safe_amount, MAX_SAFE_INTEGER};
use crate::{
    canonical_bytes, digest_payload, verify, ActionSpec, Decision, Error, MemoryStore, Node,
    NodeKind, Policy, PolicyContext, RecordingStore, Registry, Result,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::cell::{Cell, Ref, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

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

struct StoreCell {
    inner: RefCell<Box<dyn RecordingStore>>,
    busy: Cell<bool>,
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
        }))
    }
    pub fn borrow(&self) -> Ref<'_, dyn RecordingStore> {
        Ref::map(self.0.inner.borrow(), |s| s.as_ref())
    }
    fn lock(&self) -> Result<OperationGuard> {
        if self.0.busy.replace(true) {
            return Err(Error::Busy);
        }
        Ok(OperationGuard(self.clone()))
    }
}
struct OperationGuard(SharedStore);
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
                self.store.0.inner.borrow_mut().update_meta(
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
        })
    }

    fn verified(&self, id: &str) -> Result<Node> {
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
        store.get(id)
    }

    fn structural(&self, candidate: Node) -> Result<Node> {
        if self.store.borrow().exists(&candidate.id) {
            return self.verified(&candidate.id);
        }
        if self.mode == ReplayMode::Replay {
            return Err(Error::MissingRecording(candidate.id));
        }
        self.store.0.inner.borrow_mut().put(candidate.clone())?;
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
        let _guard = self.runtime.store.lock()?;
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
            return self.call(NodeKind::ToolCall, payload, args, options, |_| {
                unreachable!("strict replay never dispatches")
            });
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
            && self.runtime.store.borrow().exists(&candidate.id)
        {
            return Err(Error::DuplicateRecording(candidate.id));
        }
        if self.runtime.mode == ReplayMode::Hybrid
            && self.runtime.store.borrow().exists(&candidate.id)
        {
            return self.call(NodeKind::ToolCall, payload, args, options, |_| {
                unreachable!("recorded result reused")
            });
        }
        let mut confirmation_required = false;
        for policy in self.runtime.policies.clone() {
            let ctx = PolicyContext {
                spec: spec.clone(),
                args: args.clone(),
                cursor_id: self.cursor_id.clone(),
                run_label: self.label.clone(),
                counters: self.spent()?,
            };
            match policy(&ctx) {
                Decision::Allow => {}
                Decision::Deny => return self.refuse_policy("denied by policy".into(), &payload),
                Decision::Confirm => confirmation_required = true,
            }
        }
        if confirmation_required {
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
        let Some(handler) = spec.handler() else {
            return self.refuse_policy("registered action has no handler".into(), &payload);
        };
        self.call(NodeKind::ToolCall, payload, args, options, move |args| {
            handler(args)
        })
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
        })
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
        let mut total = Charges::default();
        let mut unknown = false;
        for node in self.runtime.store.borrow().walk(anchor)? {
            node.validate()?;
            total.add(charges_from_meta(&node.meta)?)?;
            unknown |= node.meta.get("accounting_unknown") == Some(&Value::Bool(true));
        }
        Ok((total, unknown))
    }

    fn depth(&self) -> Result<u64> {
        let mut current = Some(self.cursor_id.clone());
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
    ) -> Result<Charges> {
        self.runtime.verified(&self.cursor_id)?;
        let call = matches!(kind, NodeKind::ModelCall | NodeKind::ToolCall);
        let estimates = Charges {
            steps: u64::from(call),
            tokens: if call {
                options.estimated_tokens.unwrap_or(0)
            } else {
                0
            },
        };
        safe_amount(estimates.tokens, "estimated tokens")?;
        let next_depth = self
            .depth()?
            .checked_add(1)
            .ok_or_else(|| Error::Integrity("depth overflow".into()))?;
        for scope in self.scopes.clone() {
            let (spent, unknown) = self.accounting(&scope.anchor)?;
            if scope.budget.tokens.is_some() && call && options.estimated_tokens.is_none() {
                return self.refuse_budget(
                    kind,
                    payload,
                    "tokens",
                    0,
                    0,
                    false,
                    "missing token estimate",
                );
            }
            if scope.budget.tokens.is_some() && unknown {
                return self.refuse_budget(
                    kind,
                    payload,
                    "tokens",
                    0,
                    0,
                    false,
                    "prior token accounting is unknown",
                );
            }
            for (meter, limit, already, requested) in [
                ("steps", scope.budget.steps, spent.steps, estimates.steps),
                (
                    "tokens",
                    scope.budget.tokens,
                    spent.tokens,
                    estimates.tokens,
                ),
                ("depth", scope.budget.depth, 0, next_depth),
            ] {
                if let Some(limit) = limit {
                    if already > limit || requested > limit.saturating_sub(already) {
                        return self.refuse_budget(
                            kind,
                            payload,
                            meter,
                            requested,
                            i128::from(limit) - i128::from(already),
                            meter == "tokens" && call,
                            "budget",
                        );
                    }
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
        requested: u64,
        remaining: i128,
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
        let candidate = Node::make(
            kind,
            Some(&self.cursor_id),
            options.attempt,
            payload.clone(),
            None,
            json!({}),
        )?;
        if self.runtime.store.borrow().exists(&candidate.id) {
            if self.runtime.mode == ReplayMode::Record {
                return Err(Error::DuplicateRecording(candidate.id));
            }
            let node = self.runtime.verified(&candidate.id)?;
            if node.result_text.is_none()
                || node
                    .meta
                    .get("state")
                    .and_then(Value::as_str)
                    .is_some_and(|s| s != "completed")
            {
                return Err(if self.runtime.mode == ReplayMode::Replay {
                    Error::MissingRecording(node.id)
                } else {
                    Error::DuplicateRecording(node.id)
                });
            }
            self.avoided.add(charges_from_meta(&node.meta)?)?;
            self.cursor_id = node.id.clone();
            return Ok(node);
        }
        if self.runtime.mode == ReplayMode::Replay {
            return Err(Error::MissingRecording(candidate.id));
        }
        let estimates = self.precheck(kind, &payload, options)?;
        let pending = Node::make(
            kind,
            Some(&self.cursor_id),
            options.attempt,
            payload.clone(),
            None,
            json!({"state":"pending","charges":estimates,"accounting_unknown":true}),
        )?;
        self.runtime
            .store
            .0
            .inner
            .borrow_mut()
            .put(pending.clone())?;
        self.cursor_id = pending.id.clone();
        // A panic leaves the durable pending marker and conservative charges.
        let outcome = handler(handler_args);
        let token_budget = self.scopes.iter().any(|s| s.budget.tokens.is_some());
        let result = match outcome {
            Ok(result) if result.is_object() => result,
            Ok(_) => {
                self.fail_pending(&pending, estimates, "invalid_result")?;
                return Err(Error::Handler("callback must return a JSON object".into()));
            }
            Err(e) => {
                self.fail_pending(&pending, estimates, "handler_error")?;
                return Err(e);
            }
        };
        let actual = usage_tokens(&result);
        let unknown = actual.is_none();
        let charges = Charges {
            steps: 1,
            tokens: actual.unwrap_or(estimates.tokens),
        };
        let state = "completed";
        let node = Node::make(
            kind,
            pending.parent.as_deref(),
            options.attempt,
            payload,
            Some(result),
            json!({"state":state,"charges":charges,"accounting_unknown":unknown}),
        )?;
        self.runtime
            .store
            .0
            .inner
            .borrow_mut()
            .finalize(node.clone())?;
        if unknown && token_budget {
            return Err(Error::UsageError { node_id: node.id });
        }
        Ok(node)
    }

    fn fail_pending(&self, pending: &Node, charges: Charges, reason: &str) -> Result<()> {
        let failed = Node::make(
            pending.kind,
            pending.parent.as_deref(),
            pending.attempt,
            pending.payload.clone(),
            None,
            json!({"state":"failed","charges":charges,"accounting_unknown":true,"error":reason}),
        )?;
        self.runtime.store.0.inner.borrow_mut().finalize(failed)
    }
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
        let n = value
            .as_u64()
            .ok_or_else(|| Error::Integrity(format!("{key} charge must be nonnegative integer")))?;
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
