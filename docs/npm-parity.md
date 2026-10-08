# npm parity with Python Pollard 1.6.0

The npm 0.2.0 release targets the published PyPI `pollard==1.6.0` release,
uploaded September 1, 2026. The reference wheel is
`pollard-1.6.0-py3-none-any.whl`, SHA-256
`569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f`.
All Python source files used as the implementation reference match this wheel.
No unpublished changes from another checkout were used.

The native package keeps its own version line, `pollardai`. npm 0.2.0 was
published on October 8, 2026 and verified through a clean public-registry
installation. The Rust port remains at 0.1.0. This matrix describes capabilities,
not a claim that every Python import or storage format has an identical
JavaScript API.

| Python release capability | npm 0.2.0 implementation | Evidence / boundary |
|---|---|---|
| Content-addressed trees, registry digests and redaction | `Node`, identity functions, `Registry`, `ActionSpec` | Frozen Python vectors; portable safe-integer identity subset |
| Sync and async runtime | `Runtime`, `Run`, sync methods and `*Async` methods | Node test runner; no separate Python `AsyncRuntime` class |
| Streaming and retained chunk replay | Iterable/async iterable handlers, `onDelta`, `keepChunks` | Stream completion, callback errors, cancellation, replay tests |
| Steps, depth, tokens, wall, USD and custom meters | Exported meter classes and `Budget.extra` | Decimal settlement, estimated fallback, precheck refusal, overshoot tests |
| Sliding windows, shared reservations and lease renewal | SQLite and transactional remote stores | Competing processes, exact 0.1+0.1+0.1 ceiling, worker heartbeat tests |
| Registry policy, confirmation and dry runs | `toolCall`, `confirm`, async variants, policies | Deny precedence, single-use tokens, redaction, immutable args |
| Generated schema local references | `resolveLocalRefs` and automatic registry expansion | Local references, pointer escaping, cycles, fail-closed unsupported schemas |
| Provider adapters | OpenAI Responses/Chat, Anthropic Messages, Bedrock Converse, compatible completion proxy | Python-normalizer fixtures plus terminal-error and usage tests; caller owns SDK |
| Prompt estimation | `OpenAITokenEstimator` | Optional `js-tiktoken` or injected tokenizer; same textual-leaf/family fallback |
| Live provider token count | Anthropic/Bedrock `estimateInputTokens` | Explicit async preflight request; pass the result as `tokenEstimate` |
| Local GPU energy | `EnergyMeter` and measurement lifecycle | Injected NVML binding; whole-GPU counters, sampled fallback; no hosted energy claim |
| Tokenmaster governance | Token/profile/USD meters and exclusive token categories | Explicit synchronous `TokenmasterClient`; no bundled Python profile catalogue |
| Replay contracts and revalidation | `ReplayContract`, comparators, `revalidateModelCall*` | Python comparator fixtures, value-free differences, distinct live evidence |
| In-memory storage | `MemoryStore` | Detached state, deterministic walk, atomic claims/maintenance |
| SQLite | `SQLiteStore` | Python schema v3; real bidirectional result/blob/manifest/custody tests |
| Hashrope | `HashRopeStore` | Python log bytes and polynomial hash golden values; in-process only |
| PostgreSQL, Redis, MongoDB, Neo4j | Native driver workers and exact common arbiter | Real service tests, independent npm namespace, serialized state document |
| Kafka | Native KafkaJS worker and ordered audit log | Real completion/reopen/replay/competing claim tests; no shared budget arbiter |
| Seal, external custody, export/import, merge, GC | Exported governance APIs | Python-compatible sealed JSON, deterministic conflicts, rollback/tamper tests |
| Offline CLI | `pollardai` executable | SQLite inspection, escaped HTML, interchange and maintenance; remote env specs |
| MCP | `registryFromMCP` | JS SDK/legacy clients, pagination, schema expansion, result normalization |
| OpenTelemetry | `spanAttributes`, `exportSpans`, `liveSpanHook` | Explicit parent contexts, content-free spans, deep-tree cleanup |
| pytest/framework recipes | Node tests and caller-owned JS integrations | Python pytest/Pydantic/LangChain packages themselves are not imported |

## Intentional semantic differences

- The npm duplicate guard remains always enabled, as in 0.1. Python 1.6 exposes
  it through `refuse_duplicate_recordings=True` and defaults to redispatch.
  Native retries require a new `attempt`; original results are retained.
- Native dispatched failures conservatively retain estimates and unknown-outcome
  evidence. Promise cancellation cannot undo a request already sent.
- JavaScript identity numbers must be safe integers; Python accepts arbitrary
  integers. Result numbers have JavaScript precision limits. Imported result
  text is retained exactly, so it need not match JavaScript serialization.
- Missing/invalid token usage now follows Python's fallback rule: preserve a
  configured estimate, otherwise charge zero. An unestimated token budget
  cannot guarantee a pre-dispatch ceiling. Default reports now include measured
  wall time. Dry-run dispatch meters charge zero. These differ from npm 0.1.
- SQLite, seals, completed HashRope logs, and JSON subtree manifests support
  portable exchange. Remote backends deliberately use separate physical
  schemas. Native workers share budgets with each other, not with Python
  workers pointed at the same server. Transfer through sealed exports.
- SQLite uses an optional native driver: `better-sqlite3@11.10.0` for Node 20,
  or `better-sqlite3@13.0.3` for Node 22+. Node 24+ requires version 13.0.3+;
  older addons can abort during cleanup with recent Node build headers.
  Pollard rejects unsupported combinations before loading the addon.
- Native remote mutations serialize one full logical namespace, validating its
  content on every operation. MongoDB's document limit bounds a namespace;
  throughput and scale differ from Python's storage architecture. No performance
  equivalence or production failover SLA is claimed by correctness tests.
- Native remote maintenance uses optimistic transactions. A concurrent writer
  aborts the batch without partially applying it or rerunning its callback.
  Kafka has no atomic maintenance transaction or shared budget/window arbiter.
- Tokenmaster and NVML integrations use supplied JavaScript implementations.
  The npm package does not install or invoke their Python libraries. Energy
  sampling uses Node timers, so synchronous blocking reduces sampling density.
- The CLI accepts environment-backed remote connection specs. Kafka's config
  object uses KafkaJS keys. It rejects direct credential-bearing URLs and limits
  offline GC to SQLite. Use the store API for other maintenance workflows.

## Reproduce validation

From `packages/npm`:

```sh
npm ci
# On Node 24+, replace the Node 20 development driver before running tests:
# npm install --no-save --package-lock=false better-sqlite3@13.0.3
npm test
npm run smoke:pack
npm run test:remote
```

The remote script owns only its `pollard-npm-parity` Docker Compose project,
binds services to localhost, and removes that project in a `finally` block.
Set `POLLARD_DOCKER_CONTEXT=desktop-linux` on Docker Desktop when required.
Alternatively configure the `POLLARD_NPM_*` URLs used by `test/remote.test.mjs`
and run that test file directly against dedicated test namespaces.

From the repository root:

```sh
python interop/generate_vectors.py --check --check-packages
python interop/generate_npm_parity.py --check
```

The native CI workflow runs the normal suite and tarball checks on Linux,
Windows and macOS, plus a Linux job provisioning all five remote services.
Live provider calls are not required; normalized SDK events and Python-produced
fixtures exercise adapter contracts without credentials or charges.
