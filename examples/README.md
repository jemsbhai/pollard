# Pollard examples

The examples are runnable programs, not fragments. Run them from the repository
root with Python 3.10 or newer. Walkthroughs `01` through `08` and `10` through
`13` use deterministic local functions and temporary or in-memory stores; they
need no API key, cloud account, model download, or network connection.
Walkthrough `09` is opt-in and contacts only the database or broker selected by
the operator.

Install the local checkout before running them:

```powershell
python -m pip install -e .
```

## Quick offline walkthroughs

| Script | What it proves | Persistent files | Expected result |
|---|---|---|---|
| `01_governed_call.py` | One model-shaped call is metered and recorded as a node | None | A deterministic mock response and a report with token charges |
| `02_best_of_n.py` | Four branches share a parent, a local judge selects one, and losing tips are pruned | None | Best score and node prefix plus spent charges |
| `03_budget_stop.py` | An exact zero-step budget refuses before the model function runs | None | `stopped by governor` and a refusal node prefix |
| `04_firewall.py` | An unregistered action fails closed before any handler executes | None | Registry refusal details and `executed=False` |
| `05_replay_ci.py` | Strict replay serves a committed result while a sentinel live client remains unreachable | Reads the committed test recording | Replayed text and nonzero avoided charges |
| `06_phase4_benchmarks.py` | Deterministic mock checks for shared prefixes, budget refusal, and registry refusal | None | JSON with three passing experiment summaries |
| `07_phase7_storage.py` | SQLite payload interning preserves identity while changing file growth in a synthetic workload | Temporary files removed on exit | JSON points, fitted finite-range exponents, ratio, and `identity_parity: true` |
| `08_phase8_scaleout.py` | A resumed SQLite request window refuses the next call, and two disconnected stores merge cleanly | Temporary files removed on exit | JSON with a window refusal, copied count, and `verify_clean: true` |
| `10_streaming_replay.py` | Retained stream chunks are emitted live and again during strict replay without calling the model function | None | Matching live and replay chunk counts plus avoided charges |
| `11_sensitive_fields.py` | A schema-marked secret reaches its handler but only a deterministic redaction marker reaches the audit tree | None | Plaintext handling and storage checks plus a digest prefix |
| `12_dry_run_confirmation.py` | A side effect is suppressed during preview and paused for an explicit confirmation token before execution | None | Three lifecycle flags showing preview, pause, and execution |
| `13_async_workflow.py` | Async model and tool functions share the same governed budget and audit tree | None | Model text, transformed tool text, and settled charges |

Run them individually:

```powershell
python examples\01_governed_call.py
python examples\02_best_of_n.py
python examples\03_budget_stop.py
python examples\04_firewall.py
python examples\05_replay_ci.py
python examples\06_phase4_benchmarks.py
python examples\07_phase7_storage.py
python examples\08_phase8_scaleout.py
python examples\10_streaming_replay.py
python examples\11_sensitive_fields.py
python examples\12_dry_run_confirmation.py
python examples\13_async_workflow.py
```

These commands make no provider request and incur 0 USD of hosted-model spend.
The timing and storage values from the small demonstration scripts are local
diagnostics, not published performance claims. Measured public claims come only
from the formal evidence protocols.

## Walkthrough details

### Governed call

`01_governed_call.py` uses `MemoryStore`, so its tree disappears when the
process exits. Replace it with `Runtime("runs.db")` to inspect a persistent
recording. The deterministic helper returns normalized `text` and `usage`
fields just like a provider adapter.

### Branch and selection

`02_best_of_n.py` opens four sibling branch cursors at distinct attempt
numbers. Every candidate and judge call remains in the audit tree. `prune()`
marks an unwanted branch; it does not delete history. The demonstration chooses
by a deterministic hash-based score, not model quality.

### Pre-execution refusal

`03_budget_stop.py` configures `Budget(steps=0)`. The exact `StepMeter` precheck
records a refusal and raises `BudgetExceeded` before `call_model` can execute.
This is different from an approximate token estimate that can settle above a
reservation after provider spend already occurred.

### Registry firewall

`04_firewall.py` registers only `approved@1` and then requests an unknown
`delete_everything` action. The registry records a `PolicyViolation` refusal.
The example's global flag demonstrates that no handler ran. The action name is
illustrative; the script performs no file operation.

### Strict replay

`05_replay_ci.py` opens
`tests/pollard_recordings/test_replay_ci.db` in replay mode. Its replacement
client raises immediately if called, so a successful command proves that the
stored semantic result was used. Change the payload and replay should fail with
`MissingRecording` instead of making a live request.

### Acceptance demonstrations

`06_phase4_benchmarks.py` is mock-only historical acceptance coverage.
`07_phase7_storage.py` generates larger temporary SQLite files and can take
longer than the first five scripts. `08_phase8_scaleout.py` exercises the shared
transaction contract with SQLite; PostgreSQL multi-host behavior is covered by
the formal contention runner and CI service job.

### Streaming and strict replay

`10_streaming_replay.py` consumes a deterministic stream, forwards each chunk
to a callback, and retains the chunks on the result. A second runtime opens the
same in-memory store in strict replay mode. Its replacement model function
raises if called, so matching callback output demonstrates that replay came
entirely from the recording.

### Sensitive registry fields

`11_sensitive_fields.py` marks a registry schema's `token` property as
`sensitive`. The registered handler receives the original value at execution
time, while the node payload contains only a domain-separated digest marker.
The script also checks every stored payload and does not print the example
secret.

### Dry-run and confirmation

`12_dry_run_confirmation.py` first records a side-effectful registered action
with `dry_run=True`, proving the handler was suppressed. It then applies a
policy that returns `Decision.CONFIRM`, catches the resume token, and explicitly
confirms the pending action. Confirmation tokens are in-memory capabilities for
the active run; applications decide how an authorized human or service grants
them.

### Async workflow

`13_async_workflow.py` uses `AsyncRuntime`, `amodel_call`, and `atool_call`
inside an async context manager. Async execution changes the calling convention,
not the budget, identity, registry, replay, or audit semantics.

## Configured distributed-store walkthrough

`09_distributed_stores.py` performs one deterministic record, strict replay,
tree verification, and seal operation against PostgreSQL, Redis, MongoDB,
Neo4j, or Kafka. It makes no model-provider request and has zero hosted-model
cost. It does make live database or broker requests and leaves the example run
in the selected logical store.

Install the selected extra and set only its connection environment variables.
For example, Redis uses:

```powershell
python -m pip install -e ".[redis]"
$env:POLLARD_REDIS_URL = "rediss://user:password@redis.example:6379/0"
python examples\09_distributed_stores.py `
  --backend redis `
  --store-id example-team
```

The other backend selectors and required variables are:

| Backend | Install extra | Required environment |
|---|---|---|
| `postgresql` | `pollard[pg]` | `POLLARD_PG_DSN` |
| `redis` | `pollard[redis]` | `POLLARD_REDIS_URL`; optional `POLLARD_REDIS_PREFIX` |
| `mongodb` | `pollard[mongodb]` | `POLLARD_MONGODB_URI`; optional `POLLARD_MONGODB_DATABASE` |
| `neo4j` | `pollard[neo4j]` | `POLLARD_NEO4J_URI`, `POLLARD_NEO4J_PASSWORD`; optional user and database variables |
| `kafka` | `pollard[kafka]` | `POLLARD_KAFKA_BOOTSTRAP`, `POLLARD_KAFKA_TOPIC` |

The JSON result contains `verified: true`, `replay_matched: true`, a seal
digest, and `shared_arbiter`. The last field is true for PostgreSQL, Redis,
MongoDB, and Neo4j, but false for Kafka. The one-step budget remains settled in
transactional stores after the command completes. Reusing the same run label
is therefore refused before the offline callable executes. Pass a fresh
`--run-label` for each new recording; the strict replay is already performed
inside the same invocation.

Kafka's topic must already satisfy Pollard's partition and retention checks.
The complete provisioning rules, security boundary, lifecycle, uncertainty
handling, migration example, and recovery checklist are in the
[distributed-store operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md).
Applications that discover a Redis primary through Sentinel use the
caller-owned factory example in the
[Redis operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/redis-operations.md).

## Helper programs

| File | Role |
|---|---|
| `mock_model.py` | Deterministic model and judge functions imported by walkthroughs |
| `mcp_demo_server.py` | Credential-free MCP stdio server used by the MCP recipe |
| `_exp006_common.py` | Local llama.cpp, hashing, artifact, and adapter helpers shared by EXP-006 recording scripts |
| `__init__.py` | Marks the directory as importable for tests and shared helpers |

Do not run `mcp_demo_server.py` by itself from a terminal expecting normal
output. It speaks MCP JSON-RPC over stdio and is launched by the command in the
[MCP recipe](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md#mcp-registry-firewall).

## Formal evidence runners

The `exp_*` scripts implement controlled protocols. They validate prerequisites,
record environment and artifact hashes, and fail when a registered condition
does not hold. They do not call OpenAI, Anthropic, Azure, Bedrock, Vertex AI, or
another hosted model provider.

| Script | External prerequisite | Network behavior | Primary artifact |
|---|---|---|---|
| `exp_001_local_model.py` | NVIDIA GPU with NVML support, pinned llama.cpp server and archive, local GGUF model | Loopback llama.cpp only | `evidence/EXP-001/local-model-result.json` |
| `exp_004_storage.py` | Local filesystem and SQLite | None | `evidence/EXP-004/result.json` |
| `exp_005_contention.py` | At least two labeled PostgreSQL targets | Database connections to supplied DSNs | `evidence/EXP-005/result.json` |
| `exp_006_research.py` | Pinned llama.cpp server and local model | Loopback llama.cpp only | Research recording, outcome, seal, and HTML tree |
| `exp_006_code_fix.py` | Pinned llama.cpp server and local model | Loopback llama.cpp only | Code-fix recording, outcome, seal, fixed file, and HTML tree |
| `exp_006_mcp_household.py` | Pinned llama.cpp server, local model, and `pollard[mcp]` | Loopback llama.cpp plus local stdio MCP | Household recording, outcome, seal, and HTML tree |
| `exp_006_verify.py` | Python and the repository checkout | None | Verification report; optional combined manifest rewrite |

### EXP-001 local inference

Use the exact runtime archive and model hash expected by the protocol:

```powershell
python examples\exp_001_local_model.py `
  --server-binary <path-to-llama-server.exe> `
  --runtime-archive <path-to-release-archive.zip> `
  --model <path-to-model.gguf> `
  --model-id <model-name> `
  --llama-release <release> `
  --expected-runtime-sha256 <archive-sha256> `
  --expected-model-sha256 <model-sha256> `
  --output evidence\EXP-001\local-model-result.json
```

The runner measures wall-clock time and whole-GPU NVML energy in its recorded
environment. Its USD field applies a declared electricity comparison rate; it
is not a cloud bill, utility bill, or total-cost measurement.

### EXP-004 storage curves

```powershell
python examples\exp_004_storage.py --output evidence\EXP-004\result.json
```

The runner builds fresh databases for five seeds at four turn counts with
interning enabled and disabled. The fitted exponents describe only that finite
synthetic range; they do not establish an asymptotic complexity class.

### EXP-005 PostgreSQL contention

```powershell
python examples\exp_005_contention.py `
  --target "pg14=$env:POLLARD_EXP_PG14_DSN" `
  --target "pg18=$env:POLLARD_EXP_PG18_DSN" `
  --output evidence\EXP-005\result.json
```

The DSNs are used for live database connections and excluded from the result.
Use isolated databases: the runner creates and mutates Pollard schema state.
This is a same-host correctness protocol, not a throughput, availability,
network-partition, or consensus benchmark.

### EXP-006 case studies

Verification of the committed artifacts is the preferred reviewer path. It
needs no optional dependency, local model, MCP server, credential, or network:

```powershell
$env:PYTHONPATH = (Resolve-Path src)
python examples\exp_006_verify.py
```

Expected top-level fields are `ok: true`, `network_used: false`,
`model_calls_executed: 0`, and `tool_calls_executed: 0`. The three case rows
each report `paths_replayed: 2`; their node counts sum to 49.

Re-recording is environment-specific and can change node IDs, timings, model
text, seals, and hashes:

```powershell
python examples\exp_006_research.py --server-binary <llama-server> --model <model>
python examples\exp_006_code_fix.py --server-binary <llama-server> --model <model>
python examples\exp_006_mcp_household.py --server-binary <llama-server> --model <model>
python examples\exp_006_verify.py --write-manifest
```

Review the [evidence index](https://github.com/jemsbhai/pollard/blob/main/evidence/README.md)
and [EXP-006 case-study index](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/README.md)
before replacing a committed result. The research synthesis is model-generated.
The code-fix and household candidate controllers are deterministic with model
review; those artifacts do not prove autonomous candidate invention.

## Live providers and frameworks

OpenAI, Anthropic, Azure OpenAI, Amazon Bedrock, other LiteLLM cloud routes,
LangGraph, pydantic-ai, and MCP have separate, opt-in
[integration recipes](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md).
Those provider recipes can incur charges and are never part of offline example
or CI execution.
