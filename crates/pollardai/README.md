# pollardai (Rust)

Native Rust runtime for Pollard governed execution trees. Version 0.2.1 retains the behavior and storage formats added in 0.2.0 for **PyPI `pollard==1.6.0`**, verified against its SHA-pinned wheel. It needs no Python installation at runtime. The runtime, audit workflows, provider normalization and all eight storage backends have native implementations. Language and integration boundaries are described below. Rust 0.1.0 has a smaller API.

Version 0.2.1 corrects test-fixture timing without changing production behavior; see the [release status and verification receipts](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1).

```sh
cargo add pollardai@0.2.1
```

```rust
use pollardai::{json, Budget, CallOptions, ReplayMode, Result, Runtime};

fn main() -> Result<()> {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("first-run", Some(Budget {
        tokens: Some(10), steps: Some(1), ..Default::default()
    }), 0)?;
    let payload = json!({"model":"local-demo","prompt":"hello"});
    let recorded = run.model_call(payload.clone(), CallOptions::default(), |_| {
        Ok(json!({"text":"offline reply", "usage":{
            "input_tokens":2, "output_tokens":4
        }}))
    })?;
    assert_eq!(run.spent()?.tokens, 6);
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut cached = replay.run("first-run", None, 0)?;
    assert_eq!(cached.model_call(payload, CallOptions::default(), |_| {
        panic!("strict replay cannot dispatch")
    })?.id, recorded.id);
    Ok(())
}
```

## Runtime behavior

- `Record` executes completed duplicate identities by default, preserves the first result, and records conflicts, matching Python 1.6.0. Opt into pre-dispatch duplicate refusal with `with_refuse_duplicate_recordings(true)`. Change `CallOptions.attempt` for a distinct identity. Recorded pending or uncertain identities are refused until explicitly reconciled. Crash fencing depends on the backend: some staging is transient or absent, so this is not a universal durable dispatch guarantee.
- `Hybrid` reuses verified recordings; registered policies are evaluated before reuse. `Replay` verifies identity/result bytes and ancestors, performs no writes, and never invokes live callbacks or policies.

Native hybrid hits also verify ancestors; Python 1.6.0 invokes that explicit verification pass only for strict replay. SQLite caches verified prefixes using local revisions and external commit tokens, rechecking those tokens around reads and writes. External modifications invalidate the cache; triggers and unsupported schemas disable it. Sustained concurrent commits use a consistent read snapshot after the optimistic cache retries, without promoting that historical snapshot into the live cache.
- Token estimates are optional. Actual usage comes from nonnegative integer `usage.input_tokens` and `usage.output_tokens`. Invalid or absent usage falls back to an available estimate with `accounting_fallbacks` metadata, or records no token charge. Overshoot retains the completed result and gates later work.
- Branches share charges; rollback does not refund work. `resume`, `prune`, `report`, dry-run registered actions, node observers, confirmation tokens, and explicit model revalidation are supported. Observation records do not replace the original recording.
- Ordinary callback errors before any stream output clean up the reservation. `Error::OutcomeUnknown` preserves conservative accounting when provider dispatch may have taken effect. Panics and dropped async futures also conservatively settle dispatch estimates. Check `is_post_dispatch_outcome_unknown()` before deciding whether an application-level retry is safe.

`Run::amodel_call` and `atool_call` await caller-owned futures with the same gates. `ActionSpec::with_async_handler`, `registered_tool_call_async`, `confirm_async`, and `revalidate_model_call_async` cover governed async actions and live comparisons. Gates run before constructing provider futures. `AsyncRuntime`/`AsyncRun` are convenient wrappers; no executor is imposed. Shared handles use `Rc` and operate on one thread. Independent SQLite runtimes can run on separate threads/processes with shared reservations. Arbitrary callbacks are not automatically moved to worker threads.

`model_stream`, `tool_stream`, `amodel_stream`, and `atool_stream` merge chunk dictionaries using the Python contract: `result` replaces the accumulator; `delta` merges recursively; strings append; arrays concatenate. Retained chunks are reemitted during replay. Unretained chunks are discarded immediately after merging. Provider adapters expose both lazy iterator normalization and incremental `StreamNormalizer` for async transports.

`revalidate_model_stream` and `revalidate_model_stream_async` apply the same streaming contract to separately recorded live observations. Their comparator variants support custom comparison, `RevalidationOptions.keep_chunks` enables chunk retention, and governance gates run before constructing the stream. Provider callbacks receive the original live payload, without observation metadata.

## Meters, storage and audit

`Budget` retains the existing integer step/token/depth API. `run_with_meters` and `MeterBudget` add `usd`, `seconds`, and custom limits. `Meter` defines precheck estimates and actual charges; `MeterPrecheckRefusal` distinguishes expected governance refusals from configuration errors. Included meters cover steps, depth, wall time, tokens with an estimator, caller-supplied decimal model prices, sliding windows, and numeric metadata measurements. Decimal arithmetic is used for cost formulas, accumulation, budget comparisons and SQLite arbitration. `ModelPrice` takes decimal strings; pricing is never silently fetched from a service.

`parse_decimal_exact`, `checked_decimal_add` and `checked_decimal_subtract` reject unrepresentable significant digits. Cost formulas retain exact intermediate digits and reject an unrepresentable final result: `6e-23` dollars per million tokens for one token returns an error instead of rounding `6e-29` to `1e-28`. Intermediate overflow alone does not reject an otherwise representable cost.

For cross-language low-level reservation retries, `reserve_decimal_text` and `settle_decimal_text` accept `TextBudgetReservation`, `TextWindowReservation`, and string-valued charge maps. These methods preserve Python Decimal spellings such as `1E+2`, `-0.00`, and `1.2300` in request/settlement fingerprints. They are available on SQLite, PostgreSQL, Redis, MongoDB, and Neo4j stores; SQLite retains its schema-v3 duplicate-reservation semantics. `reservation_request_text` and `reservation_charges_text` expose the same wire encodings without I/O. Existing Decimal-based methods remain available, but cannot recover a positive exponent already discarded by the caller's numeric type.

`MeterMeasurement` supplies start, finish and readings hooks with reverse-order cleanup, including panic/future cancellation. Secondary cleanup errors are available through `Runtime::cleanup_errors`. `EnergyMeter` accepts power samples or a cumulative millijoule counter; feature `nvml` adds `nvml_energy_meter` for explicit local NVIDIA device sampling. Device readings include other GPU work and do not establish energy savings.

The `tokenmaster` module implements the Tokenmaster 0.2 profile, alias, limit, output calibration, pricing-tier, cache/reasoning usage, gauge and advisor contracts used by Pollard 1.6.0. Supply an explicit `ProfileRegistry`; native code does not fetch or silently update the Python package's catalog. `tokenmaster_governance_meters` creates the context/output and cost meters. Feature `estimate-openai` provides the offline BPE `OpenAiTokenEstimator`; its embedded vocabulary is checked against Python tiktoken fixtures.

`SQLiteStore` reads and writes Python schema-v3 databases, including interned payload strings and literal blob-reference objects. Imported result text is retained byte-for-byte. `open_read_only` does not create or repair databases. Runtime budget/window reservations, lease renewal and settlement use independent SQLite transactions; separate Python and Rust connections share the same ledgers. Lease loss or uncertain settlement is reported as an unknown outcome, rather than an ordinary retryable failure.

`SQLiteStore::migrate_legacy` explicitly migrates schema 0, 1 or 2 inside a transaction after validating the recording. Ordinary open remains non-migrating. Back up databases before an intentional schema migration.

Optional `PostgresStore`, `RedisStore`, `MongoStore` and `Neo4jStore` implement the release's persistent records, exact reservation ledgers, leases, tombstones and atomic shared budget/window arbitration. PostgreSQL uses native NUMERIC values and row locks; the other three implement the common Python KV wire format. MongoDB uses a dedicated client worker, allowing calls from inside an async executor. Neo4j supports direct and routed URIs with shared bookmarks. Live standalone-service tests and Python-to-Rust-to-Python checks cover these backends; they are not cluster failover or performance benchmarks.

For mixed Python/Rust MongoDB leases and windows, construct the Python 1.6.0 store with `tz_aware=True`. Its default naive BSON datetime conversion treats server UTC as local time on non-UTC hosts; native code uses the actual UTC epoch. The interoperability suite records this release bug and verifies the timezone-aware configuration.

`KafkaStore` uses one pre-existing partition with unlimited retention, deterministic operation IDs, acknowledgements and reconnect validation. It supports ordered audit/replay, without a shared budget arbiter. `HashRopeStore` is an in-process append-only byte log whose polynomial hash and operation bytes match Python; it is not a multi-process database.

`MemoryStore` indexes children and exposes immutable-identity/revision guarantees to the runtime. Verified ancestry prefixes and incremental totals avoid repeated scans. Custom backends default to full verification unless they explicitly provide those guarantees. Mutable metadata remains outside identity and result digests and requires trusted storage/operators.

`seal`, `verify_subtree`, `export_subtree`/`import_subtree`, `merge`, explicit `gc`, and a separate append-only `SQLiteSealSink` support native audit workflows. Import validates identities, exact result text, ordering and seals before writing. SQLite collision checks and writes share a transaction. Merges preserve first results and collect metadata/result conflicts.

## Identity, schemas and adapters

The frozen domains remain `pollard/v1`, `pollard/v1:result`, `pollard/v1:redact` and `pollard/v1:seal`. Canonical JSON accepts arbitrary-size integer payloads, sorted Unicode keys and valid UTF-8, rejecting identity floats. Native result serialization follows Python's floating-point notation; imported results are verified using their original text. Release fixtures cover large integers, Unicode, subnormal and boundary floats, schema failures, and full provider stream traces.

The registry implements Python's built-in fail-closed schema subset, including local `$ref`/`$defs`/`definitions` expansion and sensitive string commitments. Remote references and unsupported validation keywords are rejected. Registered handlers receive detached raw arguments while the audit identity contains redacted commitments. Commitments are deterministic hashes, not encryption. Unknown-tool arguments and results are not automatically redacted.

The `adapters` module normalizes OpenAI Responses/Chat, Anthropic, Bedrock and LiteLLM JSON responses and streams, including tool fragments, cached-token usage and terminal failures. Applications own SDK clients, credentials, network transports, retries and cancellation.

`mcp::McpSession` adapts caller-owned MCP tool discovery and async invocation to a governed registry. `otel::SpanExporter` exports content-free attributes and parent relationships to a caller-owned telemetry exporter, including cleanup after failures. These traits keep SDK versions and transport configuration with the application. There is no implicit network connection.

## Optional features

| Feature | Native capability |
| --- | --- |
| `postgres` | PostgreSQL storage; use a caller-supplied connection factory for TLS configuration |
| `redis` / `redis-tls` | Redis storage; `redis-tls` adds OS trust and hostname verification for `rediss://` |
| `mongodb` | MongoDB storage and transactional KV operations |
| `neo4j` | Bolt and routed Neo4j storage with bookmarks |
| `kafka` / `kafka-tls` | Kafka storage; TLS adds vendored OpenSSL and SASL PLAIN/SCRAM support |
| `estimate-openai` | Embedded OpenAI-family BPE token estimator |
| `nvml` | Explicit NVIDIA NVML energy sampling |

The default build includes Memory, SQLite and HashRope. Kafka builds require CMake and a C/C++ toolchain; Linux builds also need curl development headers. `kafka-tls` also requires the vendored OpenSSL build prerequisites (including Perl); it is validated on Linux with Rust 1.74. `nvml` compiles without a GPU but actual sampling requires the local NVIDIA driver/library. Live TLS authentication, replica failover and hardware across vendors have not been tested.

## CLI

`cargo run --bin pollard -- --help` lists native operator commands: `runs`, `show`, `report`, `verify`, `seal`, `export`, `import`, `merge`, and `gc`. JSON is the default output; `show --json` fields match the released Python tree document. `show --ascii`, `--unicode` and `--html FILE` render trees. Payload display requires `show --payloads`. Inspection uses a read-only connection; `import` and `gc` are SQLite-only. Creating an import/merge destination requires `--initialize-if-missing`.

Remote references resolve credentials from named environment variables, for example `pg-env:POLLARD_PG#team`, `redis-env:POLLARD_REDIS?prefix=pollard#team`, `mongo-env:POLLARD_MONGO?database=pollard&prefix=pollard#team`, `neo4j-env:POLLARD_NEO4J?user-env=NEO4J_USER&password-env=NEO4J_PASSWORD#team`, or `kafka-env:POLLARD_KAFKA?topic=pollard#team`. Kafka's variable contains a JSON client configuration object. Enable the corresponding Cargo feature. Inline connection URLs are rejected. Merge verifies every source before destination writes; read-only commands do not initialize missing stores.

## Compatibility boundary

The native APIs provide equivalent behavior where Rust and Python have corresponding concepts. Python decorators, Pydantic model introspection, pytest hooks and framework-specific Python objects are not Rust APIs: supply JSON schemas, Rust tests, normalized callbacks and the MCP/OTel traits instead. Native provider HTTP/SDK transports remain caller-owned. Tokenmaster profiles must be supplied explicitly. CLI JSON defaults and HTML presentation differ, while identity, recording, storage and governance contracts are checked against the release.

Merge preparation uses memory instead of Python's disk spool, so memory use scales with imported data. Each SQLite import/merge applies transactionally; a CLI merge across several sources is not one global transaction, and append-only backends cannot undo already acknowledged appends. Rust attempts are `u64`, exact step/token accounting retains the safe integer range, and native decimals have a finite 96-bit coefficient with scale up to 28. Decimal accounting rejects unsupported input precision and unrepresentable results instead of rounding significant digits; it does not emulate Python's wider exponent range or configurable Decimal rounding context. Text reservation fingerprints retain representable amounts' original scale/exponent, including signed zero; arithmetic involving a preferred fractional scale above 28 can still be rejected. Public `f64` meter/profile/report interfaces retain their floating-point representation limits. Panic/cancellation and post-result failures use an explicit `OutcomeUnknown` error and conservative accounting; error class names and diagnostic wording are native. Imported result text remains authoritative for cross-language digest verification.

## Validation and performance

From the repository root:

```sh
cargo fmt --manifest-path crates/pollardai/Cargo.toml --check
cargo clippy --manifest-path crates/pollardai/Cargo.toml --all-targets --locked -- -D warnings
cargo test --manifest-path crates/pollardai/Cargo.toml --locked
cargo +1.74.0 test --manifest-path crates/pollardai/Cargo.toml --locked
cargo package --manifest-path crates/pollardai/Cargo.toml --locked
```

The release compatibility matrix, Python/Rust SQLite and reservation interchange checks, exact fixture provenance, test log, reproducible benchmark harness, raw timing samples and interpretation are in [the Rust parity evidence](https://github.com/jemsbhai/pollard/tree/main/evidence/rust-parity-1.6.0). Benchmarks compare release builds with both Rust 0.1.0 and the verified PyPI 1.6.0 wheel. They measure local overhead with deterministic callbacks, not provider latency or end-to-end agent speed.

Rust 1.74+ with the checked lockfile. MIT licensed; see `LICENSE`.
