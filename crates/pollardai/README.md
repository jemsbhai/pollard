# pollardai (Rust)

Experimental **0.1.0 native Rust core** for [Pollard](https://github.com/jemsbhai/pollard), the Python package for governed execution trees. No Python installation is required. This release provides synchronous callbacks, an in-memory store, integer step/token/depth budgets, detached records, branches and rollback, verified strict/hybrid replay, a frozen action registry, policy denial/confirmation, sensitive input commitments, and audit refusal records.

```toml
[dependencies]
pollardai = "0.1.0"
```

```rust
use pollardai::{json, Budget, CallOptions, ReplayMode, Result, Runtime};

fn main() -> Result<()> {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("example", Some(Budget {
        steps: Some(2), tokens: Some(100), ..Default::default()
    }), 0)?;
    let payload = json!({"model":"mock","messages":[]});
    let node = run.model_call(payload.clone(), CallOptions {
        estimated_tokens: Some(20), ..Default::default()
    }, |_| Ok(json!({"text":"hello", "usage":{
        "input_tokens":2, "output_tokens":3
    }})))?;
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut replay_run = replay.run("example", None, 0)?;
    let cached = replay_run.model_call(payload, CallOptions::default(), |_| {
        panic!("replay never invokes this callback")
    })?;
    assert_eq!(node.id, cached.id);
    Ok(())
}
```

`model_call` callbacks receive an owned payload snapshot. `tool_call` callbacks receive `{ "tool": name, "args": args }`; registered handlers receive an owned raw arguments object. All callbacks return `Result<Value>` containing a JSON object. Registry specifications are immutable, versioned, and checked before dispatch; attach a `Registry` with `Runtime::with_registry`, then use `registered_tool_call`. Attach policies with `with_policy`; `Decision::Confirm` returns a single-use token that `Run::confirm` consumes at its original cursor. Confirmation repeats budget checks immediately before dispatch.

`ReplayMode::Record` refuses an existing live call identity. Use a new `CallOptions.attempt` for a deliberate retry. `Hybrid` reuses a complete, verified recording or dispatches a missing identity. `Replay` verifies the requested node and all ancestors, performs no writes, and never invokes model/tool handlers or policies for a valid recording. Failed or pending calls never silently dispatch again.

Supply `estimated_tokens` as a conservative **total input + output** estimate when a token budget is active. Amounts are nonnegative exact integers no greater than 9,007,199,254,740,991. Actual usage comes from `usage.input_tokens` and `usage.output_tokens`; valid actual usage can exceed the estimate, and the recorded overshoot blocks later calls. Missing, negative, fractional, boolean, overflowing, or otherwise invalid usage retains the conservative estimate, marks accounting unknown, and returns `UsageError` under a token budget. Unknown accounting blocks later calls under a token budget. Callback errors charge the dispatched step and estimate; a panic leaves a pending record with those charges. An estimate is required before token-limited dispatch.

Branches share charges with the parent. `Run::branch` creates a branch note and returns a child run; `adopt` moves the parent's cursor to a descendant. Rollback moves a cursor to an ancestor and does not refund dispatched work. Depth is absolute ancestry depth, with the root at zero. Root and branch budgets include all work stored below their anchors.

`MemoryStore` returns detached copies, preserves the first complete result, and records conflicting incoming results in metadata. The seven-method `Store` trait supports read/import/verification backends; live runtimes additionally require the `RecordingStore::finalize` extension for atomic pending settlement. Custom backends must preserve its one-time pending-to-settled contract. Share a `SharedStore` handle across runtimes to share records and the process-local synchronous reentrancy lock. This API uses `Rc` and is intentionally single-threaded; it supplies no distributed dispatch lock or transactional arbiter.

Identity bytes, node/spec/registry digests, redaction commitments, and exact imported result-text digests match Python within the portable subset, tested against shared Python-generated fixtures. The domain remains `pollard/v1`, independent of the registry package name. Identity payloads reject floats, integers outside ±9,007,199,254,740,991, and invalid Unicode. Python supports larger integers; those identities cannot be represented by this port. Native result serialization may differ from Python for floating-point formatting; import Python recordings using their exact `result_text` and `Node::from_storage`, which never reserializes that text for verification. Mutable metadata is outside the identity/result digest and is not authenticated.

Schema support is deliberately fail-closed: `object`, `string`, `integer`, `boolean`, `array`, `null`, properties, required, additionalProperties (boolean), enum, anyOf, items, integer bounds, string/array lengths, and title/description/default/sensitive annotations. Unknown keywords and local/remote `$ref` are rejected. Unicode string lengths count scalar values. Sensitive string inputs are stored as deterministic content commitments; handlers receive the originals. Commitments are not encryption and can reveal low-entropy values through guessing. Results and arbitrary unknown-tool arguments are not automatically redacted.

This is **not full Python feature parity**. Persistent/remote stores, provider/framework adapters, asynchronous calls, streaming, cost/wall-clock/energy/custom/window meters, distributed reservations, sealing, merges, revalidation, resume, dry run, CLI, and framework integrations are unported. Rust 1.74+ is supported. Run `cargo run --example governed_call` for the native record/replay smoke example.

MIT licensed; see `LICENSE`.
