# Logbook

This file is append-only. New experiment plans and results are added at the
bottom. Results must not be edited to improve a claim after the run.

## 2026-07-13 EXP-001 Plan

Question: for a best-of-n tree with one shared prefix call and n suffix calls,
does measured Pollard token spend match the analytic expression `p + n*s`?

Hypothesis: with deterministic mock calls and zero output tokens, measured token
spend equals `p + n*s` for each row, while a naive rerun baseline costs
`n*(p+s)`.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Seeds: `0, 1, 2, 3, 4`.
- Branch counts: `2, 4, 8`.
- Prefix tokens: `1000 + seed*17`.
- Suffix tokens: `100 + seed*3`.
- Metrics: naive input tokens, Pollard input tokens, savings percent,
  prediction error percent.
- Local model, wall-clock, and joules: not run in this pass. No public claim
  will use those metrics.

Pass rule: every row has prediction error at or below 2 percent. Summary reports
mean and 95 percent CI across the five seeds for each branch count.

## 2026-07-13 EXP-002 Plan

Question: does a runaway loop stop before the next call after token budget
exhaustion is detected?

Hypothesis: with a token budget of 5 and deterministic calls that each settle 4
tokens, two calls execute, the third call is refused before execution, and
overshoot is at most one settle charge.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Runtime: in-memory store.
- Budget: `Budget(tokens=5)`.
- Call charge: 4 tokens.

Pass rule: refusal node kind is `refusal`, refusal meter is `tokens`, and
overshoot is less than or equal to 4 tokens.

## 2026-07-13 EXP-003 Plan

Question: does the registry firewall block an unregistered side-effect request?

Hypothesis: a request for `delete_everything` against a registry that only
contains `approved@1` records a refusal, does not execute the approved handler,
and records the registry digest in the refusal payload.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Runtime: in-memory store with a one-action registry.
- Hostile request: `delete_everything` with a local path argument.

Pass rule: `executed` is false, refusal kind is `refusal`, refusal reason is
`policy`, and the refusal payload includes `registry_digest`.

## 2026-07-13 EXP-001, EXP-002, EXP-003 Result

Command:

```powershell
python examples\06_phase4_benchmarks.py
```

Environment:

- Platform: Windows-11-10.0.26200-SP0.
- Python: 3.12.2.
- Pollard: 0.4.0.

Outcome:

- EXP-001 passed for the deterministic mock pass. Local model, wall-clock, and
  joule metrics were not run.
- EXP-002 passed.
- EXP-003 passed.

Summary:

| Experiment | Key result |
| --- | --- |
| EXP-001 n=2 | mean savings 45.352634 percent, 95 percent CI half-width 0.098301, max prediction error 0.0 percent |
| EXP-001 n=4 | mean savings 68.028951 percent, 95 percent CI half-width 0.147451, max prediction error 0.0 percent |
| EXP-001 n=8 | mean savings 79.367109 percent, 95 percent CI half-width 0.172027, max prediction error 0.0 percent |
| EXP-002 | 2 calls executed, spent 8 tokens against limit 5, overshoot 3 tokens, bound 4 tokens |
| EXP-003 | hostile tool request executed false, refusal reason policy, registry digest recorded true |

Adversary review:

- The run only supports mock-token accounting claims.
- No README performance number was added from this pass.
- No local model, wall-clock, dollar, or joule claim should cite this entry.

## 2026-07-13 REC-005 Plan

Purpose: verify each Phase 5 live recipe once against the external stack it
documents.

Protocol:

- Run the five scripts under `docs/recipes/` with user-owned provider clients
  or an MCP session.
- Record package versions, model id, exit status, root id, and redacted console
  output.
- Do not treat syntax compilation or frozen adapter fixtures as a live provider
  verification.

Status: pending user credentials and a selected MCP server. No live result is
claimed by this entry.

## 2026-07-13 REC-005 Partial Result 1

Environment:

- Platform: Windows 11.
- Python: 3.12.2.
- Pollard: 0.5.0.
- OpenAI SDK: 2.38.0.
- MCP SDK: 1.26.0.

OpenAI tool-loop attempt:

- Script: `docs/recipes/openai_tool_loop.py`.
- Model: `gpt-5.5`.
- Exit status: failed before a provider response.
- Redacted error: HTTP 429, `insufficient_quota`; the configured API project
  requires usable billing or credits.
- Root id: none emitted by the recipe.

MCP registry result:

- Client script: `docs/recipes/mcp_registry.py`.
- Server: `examples/mcp_demo_server.py`, MCP stdio transport.
- Tool: `search` with the deterministic query `pollard`.
- Exit status: passed.
- Root id:
  `896e094b73da866da189ebfe83ce12ab8c75d6d3917605cf87574e6dd142ce7a`.
- Redacted output: one successful structured match for the local Pollard
  documentation record; no credentials or external data were involved.

Remaining live checks:

- `langgraph_node.py` and `pydantic_ai_wrap.py` use the same OpenAI account and
  are held until that account has usable quota. `pydantic-ai` also remains to
  be installed for its recipe.
- `anthropic_tool_loop.py` is held because `ANTHROPIC_API_KEY` is not configured.
- REC-005 remains incomplete; this partial result does not claim the provider
  recipes passed.

## 2026-07-13 v0.5.0 Local Release Checkpoint Result

The credential-free release gates were rerun after adding the local MCP live
path:

- Full suite: 156 tests passed.
- Coverage: 91.00 percent against a 90 percent floor.
- Ruff: passed.
- Mypy strict mode: passed for 29 source files.
- Writing-standards scan: passed.
- L0+L1 core size: 1,499 nonblank, noncomment lines against the 1,500-line
  limit.
- Build: `pollard-0.5.0.tar.gz` and
  `pollard-0.5.0-py3-none-any.whl` built successfully.
- Twine validation: both distributions passed.
- Clean wheel install: `pollard[mcp]` installed in a new virtual environment,
  imported as version 0.5.0, and completed the MCP governed-call recipe against
  MCP SDK 1.28.1.

External release status at this checkpoint:

- PyPI latest remains 0.4.0; version 0.5.0 has not been uploaded.
- TestPyPI was not attempted because TestPyPI authentication is not configured
  and REC-005 is still incomplete.
- No production upload, tag, push, or GitHub release was attempted.

## 2026-07-13 REC-005 Final Result

The user added provider credits, supplied the Anthropic credential through the
Windows user environment, directed the release to skip TestPyPI, and capped
available credit at 5 USD per provider. Before paid calls, every provider recipe
was changed to disable SDK retries and limit each response to 128 output tokens.

Environment:

- Python: 3.12.2.
- Pollard: 0.5.0.
- OpenAI SDK: 2.45.0.
- Anthropic SDK: 0.116.0.
- LangGraph: 1.2.9.
- pydantic-ai-slim: 2.9.0.
- MCP SDK: 1.28.1 for the clean-environment replay of the MCP recipe.

Results:

| Recipe | Model or server | Input tokens | Output tokens | Root id | Result |
| --- | --- | ---: | ---: | --- | --- |
| OpenAI tool loop | `gpt-5.5` | 159 | 28 | `44e86b6e8de9b6ed9f3075472c4ba5d5b039c850c85b4e8fdac1005c5737dafc` | passed |
| LangGraph node | `gpt-5.5` | 10 | 128 | `083ed1d4657c7c62ced94184d1b723800cf8bdbb2b409862c14d6605a22f8bba` | passed |
| pydantic-ai wrapper | `gpt-5.5` | 10 | 128 | `2005f48739a3e4afa5d7665537806dfab02791e05723d37ac4ee1484862c41e2` | passed |
| Anthropic tool loop | `claude-sonnet-4-6` | 1,244 | 97 | `2f9120727333664e2d35d8054ad7655d71159bcafab9403728b8b39ed588afd8` | passed |
| MCP registry | local stdio server | not applicable | not applicable | `896e094b73da866da189ebfe83ce12ab8c75d6d3917605cf87574e6dd142ce7a` | passed |

Cost calculation at the standard published rates checked on 2026-07-13:

- GPT-5.5 at 5 USD per million input tokens and 30 USD per million output
  tokens: 179 input and 284 output tokens across three recipes, approximately
  0.009415 USD.
- Claude Sonnet 4.6 at 3 USD per million input tokens and 15 USD per million
  output tokens: approximately 0.005187 USD.
- Combined estimated provider cost: approximately 0.014602 USD.

Live findings and corrections:

- The Anthropic precheck initially stopped locally because `max_tokens` was
  forwarded to the current SDK's `count_tokens` method. No billable message
  request occurred on that attempt. The adapter now strips create-only fields,
  with a regression test.
- The successful Anthropic calls were stored before Windows cp1252 output failed
  on a sun symbol. Provider recipes now configure UTF-8 output. The corrected
  display was verified from the stored nodes using an intentionally invalid API
  key, so the verification could not make another paid request.

REC-005 passed. All five live recipes produced governed roots, and measured
provider cost stayed far below the user's 5 USD limit on each account.

## 2026-07-13 v0.6.0 Offline Release Checkpoint

Scope:

- Added a direct Amazon Bedrock Converse adapter with frozen non-streaming and
  streaming fixtures, tool-use assembly, normalized token usage, and opt-in
  CountTokens prechecks.
- Documented Azure OpenAI through the OpenAI v1 client path, Azure AI and
  Vertex AI through LiteLLM, and the broader cloud-provider boundary.
- Added the core observability CLI, static HTML export, and optional
  OpenTelemetry bridge.

Verification:

- Full suite: 173 tests passed.
- Coverage: 91.54 percent against a 90 percent floor.
- Ruff: passed.
- Mypy strict mode: passed for 32 source files.
- Writing-standards scan: passed.
- Wheel and source distribution: built successfully and passed Twine checks.
- Source distribution inspection confirmed that the three raw evidence JSON
  artifacts, evidence index, examples index, and API stability policy ship in
  the archive.
- Clean wheel install: imported successfully and exposed the `pollard` CLI with
  `show`, `report`, `verify`, `seal`, and `runs`.

Cloud-provider live scope:

- No AWS, Azure, Google Cloud, OpenAI, or Anthropic model request was made for
  this checkpoint.
- Bedrock behavior is fixture-tested against the documented Converse,
  ConverseStream, and CountTokens shapes.
- Azure OpenAI and LiteLLM cloud examples compile but remain live-unverified
  because no AWS, Azure, or Google Cloud credential was supplied for this work.
- Provider spend for this checkpoint: 0 USD.

## 2026-07-13 Phase 7 Storage Growth Checkpoint Plan

Status: registered before execution. This is the Phase 7 acceptance checkpoint,
not EXP-004. Phase 9 will define and run the formal EXP-004 protocol.

Question:

- Does SQLite payload interning reduce practical growth for repeated full
  message histories without changing node ids?

Hypothesis:

- For a deterministic 200-turn synthetic conversation with one new 8 KiB
  message per turn, the interning-on database will be smaller at every measured
  checkpoint than the interning-off database.
- The final node id will match between modes at every checkpoint.
- The fitted log-log growth exponent will be lower with interning enabled. No
  claim of asymptotic linearity will be made from this checkpoint.

Protocol:

- Script: `examples/07_phase7_storage.py`.
- Turns: 25, 50, 100, and 200, each built in a new SQLite database.
- Payload: full conversation history on each model-call node; each added message
  has a deterministic 8,192-byte content string.
- Conditions: `intern_payloads=True` against `intern_payloads=False`, with the
  default 1,024-byte threshold.
- Metrics: closed-database bytes, final node-id parity, size ratio at 200 turns,
  and ordinary least-squares slope over log(turns) and log(bytes).
- Environment fields: Python version, platform, and SQLite version.
- No provider, network, GPU, or credential use.

## 2026-07-13 Phase 7 Storage Growth Checkpoint Result

Status: passed. This remains an acceptance checkpoint, not EXP-004.

Environment:

- Python: 3.12.2.
- Platform: Windows 11, build 26200.
- SQLite: 3.43.1.
- Message content per turn: 8,192 bytes.
- Intern threshold: 1,024 bytes.

Results:

| Turns | Interning on, bytes | Interning off, bytes |
| ---: | ---: | ---: |
| 25 | 319,488 | 2,723,840 |
| 50 | 655,360 | 10,555,392 |
| 100 | 1,581,056 | 41,701,376 |
| 200 | 4,222,976 | 165,650,432 |

- Final node ids matched between modes at every checkpoint.
- The plain-to-interned size ratio at 200 turns was 39.225994.
- The fitted log-log slope was 1.244381 with interning and 1.976118 without
  interning.
- The interning-on database was smaller at every checkpoint, and its fitted
  slope was lower, so all registered hypotheses passed.
- No provider, network, GPU, or credential use occurred. Provider spend was
  0 USD.

Interpretation:

- This synthetic checkpoint shows practical reduction for repeated large
  message strings under the stated setup.
- It does not establish an asymptotic growth class. Placeholder and message-list
  structure still grow with repeated histories. EXP-004 will define the formal
  protocol and fitted-model analysis in Phase 9.

## 2026-07-13 v0.7.0 Local Release Checkpoint

Scope:

- Added transparent SQLite payload interning, enabled by default with a
  configurable byte threshold and a schema-one migration path.
- Added redact-before-hash markers and automatic sensitive string handling for
  sync and async registered tool calls.
- Added explicit drop-pruned and compact garbage collection with survivor
  seals.
- Added sealed subtree export and verify-before-write import APIs and CLI
  commands.
- Added field-level data-governance documentation and an automated rule that
  README links use absolute URLs for PyPI rendering.

Acceptance evidence:

- The shared store suite passed against memory, hashrope, SQLite with interning,
  and SQLite without interning.
- Plaintext scans passed for every built-in store backend while the registered
  handler received the original sensitive argument.
- The GC property test retained every unmarked sibling across generated prune
  patterns.
- Tampered payloads, results, seals, detached parents, conflicts, and malformed
  subtree topology were rejected before import writes.
- The 200-turn storage checkpoint passed with identity parity; its scoped
  measurements are recorded in the preceding logbook entry.

Verification:

- Full suite: 211 tests passed.
- Coverage: 91.74 percent against a 90 percent floor.
- Ruff: passed.
- Mypy strict mode: passed for 34 source files.
- Writing-standards and README absolute-link scans: passed.
- Wheel and source distribution: built successfully and passed Twine checks.
- Clean wheel install: imported version 0.7.0, exposed `redact`, `gc`,
  `export_subtree`, and `import_subtree`, and listed all eight CLI commands.
- No provider or cloud request was made. Provider spend: 0 USD.

## 2026-07-13 EXP-005 Draft Plan

Status: protocol drafted for Phase 9 execution. The Phase 8 release gate below
tests a narrower fixed configuration and is not the formal experiment result.

Question:

- Under shared PostgreSQL arbitration, do exact and estimated meters follow the
  documented concurrency bound as worker count, call duration, estimate error,
  and process failure vary?

Hypotheses:

- Exact step and request prechecks settle no more than their configured limit.
- For estimated token charges, settled spend is no more than the limit plus the
  sum of positive actual-minus-estimate differences for calls admitted before
  the limit became unavailable.
- A process terminated after reservation stops consuming capacity after its
  lease expires.

Planned conditions:

- PostgreSQL major versions: current supported minimum and latest CI version.
- Worker processes: 2, 4, and 8.
- Exact limits: steps and requests with at least 30 seeded rounds per condition.
- Estimated meter: deterministic synthetic token estimates with actual charge
  errors below, equal to, and above the reservation.
- Failure legs: terminate one process after reserve and before settle at three
  lease durations.
- Metrics: admitted calls, settled amount, active reservations, expired
  reservations, refusal count, bound slack, and database errors.
- The test function remains local and deterministic. No model API is required.

Pass rules:

- Every exact-meter condition settles at or below its limit.
- Every estimated-meter condition satisfies the stated overshoot inequality.
- Every abandoned reservation releases by the first precheck after expiry.
- Any database or worker error fails the affected condition and remains in the
  raw result.

## 2026-07-13 v0.8.0 Scale-Out Acceptance Checkpoint

Scope:

- Added conservative conflict-aware merge, optional PostgreSQL storage,
  store-backed sliding windows, and transactional budget reservations.
- Added multi-store CLI forms and a PostgreSQL CI service job.
- This checkpoint validates release invariants; it does not replace EXP-005.

Environment:

- Platform: Windows 11, build 26200.
- Python: 3.12.2.
- PostgreSQL: 16 Alpine container under Docker Desktop 25.0.3.
- psycopg: 3.3.4 with the binary package.
- Provider and cloud calls: none.

Acceptance evidence:

- Two spawned operating-system processes contended on one PostgreSQL logical
  store with `Budget(steps=4)`. Exactly four functions executed in each of 20
  rounds.
- Two threads contended on `WindowMeter("requests", 3, 60)`. Exactly three
  functions executed against SQLite and exactly three against PostgreSQL.
- An intentionally abandoned SQLite reservation blocked capacity before lease
  expiry and returned capacity on the first precheck after expiry.
- A merge copied 1,001 nodes between interned SQLite stores, passed
  verification, and returned byte-identical canonical payloads for every node.
- Merge property tests covered idempotence, verify-clean union, conservative
  metadata conflicts, result conflicts, and replay rejection.

Release verification:

- Main suite: 229 passed and 5 PostgreSQL-only tests skipped when the DSN was
  absent.
- Coverage: 91.28 percent against a 90 percent floor. The optional PostgreSQL
  module is exercised by the service job and omitted from the no-service
  coverage denominator.
- PostgreSQL service subset: 66 passed, including the two-process 20-round
  storm, two-thread window and put races, row-locked metadata patches, payload
  interning, shared protocol cases, and governance operations.
- Ruff: passed for source, tests, and examples.
- Mypy strict mode: passed for 37 source files.
- Writing-standards and README absolute-link scans: passed.
- Wheel and source distribution: built successfully and passed Twine checks.
- Clean wheel install with `pollard[pg]`: imported version 0.8.0, connected to
  PostgreSQL, completed a governed call, and exposed the nine-command CLI.

Cost and claim boundary:

- OpenAI spend: 0 USD.
- Anthropic spend: 0 USD.
- AWS, Azure, Google Cloud, and other model-provider spend: 0 USD.
- The 20-round result supports the exact fixed release gate only. It is not a
  throughput, availability, or fully decentralized coordination claim.

## 2026-07-13 EXP-001 Local-Model Protocol

Status: registered by the Phase 9 roadmap before execution; exact executable
conditions were fixed in `examples/exp_001_local_model.py` before timed runs.

Question:

- Does storing a common model-generated prefix once reduce local inference
  wall-clock, token volume, whole-GPU energy, and electricity-rate cost when
  producing 2, 4, or 8 suffix branches?

Protocol:

- Runtime: llama.cpp b9630, one parallel slot, prompt caching disabled, with
  archive SHA-256 and `llama-server --version` output recorded.
- Model: local Qwen2.5-Coder 7B GGUF with file size and SHA-256 recorded.
- Conditions: naive full-prefix replay for every branch against one stored
  prefix followed by suffix branches.
- Branch counts: 2, 4, and 8. Seeds: 0 through 4. Condition order is randomized
  and counterbalanced in a recorded schedule.
- Measurements: condition-call wall-clock, generated-token counts, and raw
  whole-GPU cumulative NVML energy. Model load, warmup, and idle-baseline time
  are excluded from wall-clock.
- Cost: raw NVML joules divided by 3,600,000 and multiplied by the committed
  `evidence/prices.toml` USD/kWh rate. This excludes the host, cooling, capital,
  labor, and all other total-cost components.
- Statistics: per-condition means and two-sided 95% Student t confidence
  intervals with four degrees of freedom.

Pass rules:

- Every naive/shared output digest pair matches.
- Every llama.cpp response reports zero cached prompt tokens.
- Mean shared-prefix wall-clock, raw NVML joules, declared-rate USD, and token
  count are below the naive condition for every branch count.

## 2026-07-13 EXP-001 Local-Model Result

Status: passed.

Environment:

- Platform: Windows 11, build 26200; Python 3.12.2; Pollard 0.8.0 under test.
- GPU: NVIDIA GeForce RTX 4090 Laptop GPU, 17,171,480,576 bytes reported
  memory, driver 595.79.
- Energy source: `nvmlDeviceGetTotalEnergyConsumption`, millijoule counter,
  whole-GPU scope including other processes.
- llama.cpp: b9630, version 9630 at commit `8ed274ef4`; release archive SHA-256
  `cbb2a0b1c2459897560a654ed8dd2a816cd3989b81f9e019afd4859964794b7b`.
- Model: `qwen2.5-coder:7b`, 4,683,074,048 bytes; SHA-256
  `60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463`.
- Declared comparison rate: 0.20 USD/kWh. The committed price-table SHA-256 is
  `a6489bf40761947e2dbd69f55d2863e3a2948f96b279a0253550e8c41430c398`.

Results:

| Branches | Mean wall-clock saving | 95% CI half-width | Mean whole-GPU NVML energy saving | 95% CI half-width | Mean token saving |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 40.052576% | 6.179807 pp | 35.227991% | 10.428756 pp | 47.211420% |
| 4 | 59.134399% | 1.276648 pp | 58.534159% | 2.936724 pp | 70.902273% |
| 8 | 68.539428% | 1.232860 pp | 67.584319% | 1.459938 pp | 82.723454% |

- Every output digest matched and every response reported zero cached prompt
  tokens.
- Mean raw condition values for 2 branches were 1.034284 seconds and
  185.6460 joules naive against 0.617693 seconds and 119.3562 joules shared.
- For 4 branches they were 1.982108 seconds and 382.7694 joules naive against
  0.810241 seconds and 158.5464 joules shared.
- For 8 branches they were 3.951259 seconds and 756.3630 joules naive against
  1.243192 seconds and 245.2388 joules shared.
- Raw JSON: `evidence/EXP-001/local-model-result.json`.
- Hosted-provider requests and spend: none, 0 USD.

Interpretation boundary:

- This supports the registered local hardware, model, runtime, prompt, and
  branch-count scope. It does not establish a hosted-provider saving or general
  performance across models and hardware.
- Energy is a raw whole-GPU counter over each condition, not isolated process
  energy. USD is a declared electricity-only conversion, not actual utility
  cost or total cost of ownership.

## 2026-07-13 EXP-004 Formal Storage-Curve Protocol

Status: registered by the Phase 9 roadmap and the earlier Phase 7 checkpoint;
the exact five-seed protocol was fixed in `examples/exp_004_storage.py` before
the formal run.

Question:

- How do closed SQLite file sizes change over a finite 200-turn synthetic full
  history when payload interning is enabled or disabled?

Protocol and pass rules:

- Create fresh databases for seeds 0 through 4 at 25, 50, 100, and 200 turns.
- Add one deterministic 8,192-byte message per turn and store the full history
  on every model-call node.
- Compare the default 1,024-byte interning threshold with interning disabled.
- Record closed database bytes and node-ID parity. Fit ordinary least squares
  over natural-log turns and natural-log bytes, then report two-sided 95%
  Student t intervals across seeds.
- Pass only if every final node ID matches, interning is smaller at every
  checkpoint and seed, and its fitted exponent is lower. Make no asymptotic
  complexity claim.

## 2026-07-13 EXP-004 Formal Storage-Curve Result

Status: passed.

Environment:

- Windows 11 build 26200, Python 3.12.2, SQLite 3.43.1, Pollard 0.8.0 under
  test, 4,096-byte pages, WAL journal mode, synchronous level 2.

Results:

| Turns | Mean interned bytes | Mean plain bytes | Plain/interned ratio |
| ---: | ---: | ---: | ---: |
| 25 | 352,256 | 2,756,608 | 7.825581 |
| 50 | 688,128 | 10,588,160 | 15.386905 |
| 100 | 1,613,824 | 41,734,144 | 25.860406 |
| 200 | 4,255,744 | 165,683,200 | 38.931665 |

- The fitted exponent mean was 1.201388 with interning and 1.970694 without.
- File sizes were deterministic across the five seeds, so each size and
  exponent confidence-interval half-width was 0.
- Every node ID matched between conditions and every pass rule held.
- Raw JSON: `evidence/EXP-004/result.json`.
- Provider, network, and GPU calls: none. Provider spend: 0 USD.

Interpretation boundary:

- These are practical file sizes and finite-range fitted curves for one
  synthetic workload. They do not prove linear or quadratic asymptotic growth.

## 2026-07-13 EXP-005 Formal Contention Result

Status: passed. The preregistered plan appears above as
`2026-07-13 EXP-005 Draft Plan`; the final runner adds explicit call-duration,
version, and seed matrices without changing its hypotheses.

Environment:

- Host: Windows 11 build 26200, Python 3.12.2, psycopg 3.3.4, Pollard 0.8.0
  under test, Docker Desktop local containers.
- PostgreSQL 14.23 image:
  `postgres@sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579`.
- PostgreSQL 18.4 image:
  `postgres@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15`.

Executed matrix:

- Exact step and request meters: 2, 4, and 8 worker processes by 0, 10, and
  50 millisecond call durations, 30 seeds per condition.
- Estimated token meter: 2, 4, and 8 workers with actual charges below, equal
  to, and above the four-token estimate, 30 seeds per condition.
- Failure recovery: intentionally terminate a worker after reserve and before
  settle with 1, 2, and 4 second leases, five seeds per condition.
- Each PostgreSQL version ran 30 conditions and 825 rounds, for 1,650 rounds
  total.

Results:

- Every condition passed with no database or worker error.
- Exact step and request conditions never settled above their configured
  limits. The largest exact settled amount was 16 against a limit of 16.
- Estimated-token overshoot maxima across condition profiles were 0, 3, and 6
  tokens. Every individual round satisfied `settled <= limit +` the sum of
  positive actual-minus-estimate errors over admitted calls; minimum bound slack
  was 0.
- Every intentionally abandoned reservation returned active capacity on the
  first post-expiry precheck. An expired reservation record remains until late
  settle handling or garbage collection; it no longer consumes capacity.
- Raw JSON: `evidence/EXP-005/result.json`.
- Model-provider calls and spend: none, 0 USD.

Implementation findings:

- Concurrent first-use initialization exposed a PostgreSQL DDL race. Schema
  creation now takes a transaction advisory lock.
- Reservation settlement exposed a request-window row-lock gap that admitted
  17 calls against a limit of 16. Settlement now locks the window scope before
  moving an active reservation to its settled event. The corrected matrix and
  repeated eight-writer regression passed.

Interpretation boundary:

- This supports the exact and estimator bounds under the recorded same-host
  PostgreSQL matrix. It is not a throughput, availability, consensus,
  network-partition, or multi-region experiment.

## 2026-07-13 Phase 9 Reviewer-Adversary Pass

Status: passed for EXP-001, EXP-004, and EXP-005 public claims; EXP-006 and the
1.0 freeze remain pending.

Review actions:

- Replaced the stale README statement that local-model evidence was unrun with
  the exact EXP-001 scope.
- Attached an experiment ID to every README and launch numeric evidence claim.
- Labeled the 4090 as a Laptop GPU and energy as raw whole-GPU NVML energy.
- Labeled USD as a declared electricity-rate scenario and prohibited utility,
  hosted-provider, amortization, and total-cost interpretations.
- Described EXP-004 exponents as finite-range fits and prohibited asymptotic
  claims.
- Described EXP-005 as same-host correctness evidence and prohibited
  throughput, availability, network-partition, multi-region, and consensus
  claims.
- Recorded the two defects discovered during evidence execution instead of
  omitting failed preliminary behavior. Only the corrected reruns are the
  formal passing artifact.
- Added automated result-state, condition-count, output-parity, secret-pattern,
  and README claim-ID checks.
- Confirmed all evidence runners used local compute or local databases. OpenAI,
  Anthropic, AWS, Azure, Google Cloud, and other provider spend was 0 USD.

## 2026-07-13 v0.9.0 Evidence Candidate Checkpoint

Scope:

- Added committed raw results and reproduction runners for EXP-001 local-model
  shared prefixes, EXP-004 SQLite storage curves, and EXP-005 PostgreSQL
  contention and estimator bounds.
- Added an evidence index, adversarial public-claim boundaries, and automated
  artifact checks.
- Published `Store` at the package root and documented the candidate 1.0
  identity, canonical serialization, Store protocol, step-function contract,
  and deprecation policy. The freeze does not begin until 1.0.0.
- Updated provider documentation and recipes against current official guidance:
  GPT-5.6 as the OpenAI default, Responses storage disabled in examples, Azure
  OpenAI v1 client setup, Bedrock CountTokens limitations and IAM boundary, and
  LiteLLM cloud routes.
- EXP-006 and the sealed 1.0 launch case study remain pending target selection.

Verification:

- Main suite: 237 passed and 6 PostgreSQL-only tests skipped without a DSN.
- Coverage: 91.21% against a 90% floor.
- Fresh PostgreSQL 16 service subset: 67 passed, including repeated
  eight-writer contention and first-use initialization.
- Ruff: passed for source, tests, and examples.
- Mypy strict mode: passed for 37 source files.
- Writing-standards scan: passed for root, docs, examples, and evidence
  Markdown.
- Absolute-link scan: passed for every repository README.
- Wheel and source distribution: built successfully and passed Twine checks.
- Clean wheel install: imported 0.9.0, exposed `Store`, and ran the nine-command
  console script.
- Built wheel metadata contained 13 Markdown links and every target was an
  absolute HTTPS URL.

Cost and credentials:

- No OpenAI, Anthropic, AWS, Azure, Google Cloud, or other model-provider call
  was made. Provider spend for Phase 9 remains 0 USD.
- No provider credential was read. The PostgreSQL release database used a
  disposable local test credential and was removed after the service suite.

## 2026-07-13 EXP-006 End-to-End Case-Study Protocol

Status: the user selected all three Phase 9 candidates instead of one. The
shared artifact contract and branch conditions were fixed in the active task
before each recording. This entry was added to the repository after the
recordings, so it is not claimed as a separately timestamped preregistration.

Question:

- Can Pollard record, inspect, seal, and later replay realistic adapter and
  tool workflows with rejected and selected branches, while a stranger can
  verify the committed evidence without the model, tools, optional
  dependencies, credentials, or network access?

Shared protocol:

- Adapter: `pollard.adapters.openai.make_chat_completions_fn` against a local
  OpenAI-compatible llama.cpp server.
- Runtime: llama.cpp b9630, SHA-256
  `a30eb024cd2bbb883cbc8151d52991c12b73efbe908b0059db740a1eee2593ae`.
- Model: local `qwen2.5-coder:7b`, SHA-256
  `60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463`.
- Network during recording: loopback HTTP for the local model and local stdio
  for MCP only. Hosted-provider requests: none.
- Each case must use a frozen registry, record a rejected branch, prune it,
  roll back, select a passing branch, pass `pollard verify`, and emit a subtree
  seal plus content-free HTML.
- The combined manifest must pin every input and artifact. Strict replay must
  cover every root-to-leaf path with sentinel model and tool functions that
  fail if invoked.

Case conditions:

- EXP-006A reads three fixed local documents. A branch that omits DOC-003 must
  fail the coverage checker; the selected branch must cite and consult all
  three documents and avoid the registered unsupported phrases.
- EXP-006B starts from a pinned incorrect `clamp` implementation and four
  pinned tests. An incomplete candidate must fail reversed-bounds handling;
  the selected candidate must pass all four tests in an isolated workspace.
- EXP-006C starts three real MCP stdio servers for catalog lookup, integer
  arithmetic, and budget policy. An over-budget order must be rejected; the
  selected order must remain within the 2,000-cent limit.

Interpretation boundary:

- EXP-006A uses model-generated synthesis.
- EXP-006B and EXP-006C use deterministic candidate controllers and model
  review. They test governed execution, tool use, branch selection, and replay;
  they do not show that the model autonomously invented those candidate
  patches or orders.
- Offline replay validates the committed semantic results. It does not claim
  that a new stochastic inference run will return identical bytes.

## 2026-07-13 EXP-006 End-to-End Case-Study Result

Status: passed.

Results:

- EXP-006A produced 19 verify-clean nodes. The narrow branch omitted DOC-003
  and failed source and citation coverage. The selected branch consulted and
  cited all three documents and passed the unsupported-phrase checker. Seal:
  `b9fb38ccc401c24e1f05b78551f6469be57ed92ed134831d44a9d8ce94e8945e`.
- EXP-006B produced 17 verify-clean nodes. The baseline failed, the incomplete
  candidate failed only reversed-bounds handling, and the selected candidate
  passed all four pinned tests. The recorded write hash equals the emitted
  fixed-file hash. Seal:
  `baa30d99d652e8740c95a730746c4928759f7082cff34a7ee64519dacd1d2dd9`.
- EXP-006C produced 13 verify-clean nodes across three MCP stdio servers. The
  2,897-cent order was rejected against a 2,000-cent limit; after rollback, the
  1,547-cent order passed with a 453-cent margin. Seal:
  `c5935a9a6b31cc4b885b884a7c232c2b950527ba6ae13de98f8832369b7d2c86`.
- The combined evidence contains 49 nodes and six root-to-leaf paths. The
  offline verifier confirmed every input and artifact hash, registry digest,
  node ancestry, and subtree seal, then strictly replayed all six paths with
  zero executed model calls, zero executed tool calls, and no network use.
- No common credential pattern, absolute local user path, or remote HTML asset
  was found. Every HTML tree remained content-free by default.
- Raw manifest: `evidence/EXP-006/manifest.json`. Case index and stranger
  instructions: `evidence/EXP-006/README.md`.
- OpenAI, Anthropic, AWS, Azure, Google Cloud, and other hosted-provider calls:
  none. Provider spend: 0 USD.

## 2026-07-13 Phase 9 Final Reviewer-Adversary and API Freeze Review

Status: passed for the 1.0 release candidate.

Review actions:

- Re-ran the existing EXP-001, EXP-004, and EXP-005 scope audit and retained
  every hardware, finite-fit, same-host, energy, cost, and non-consensus
  qualifier.
- Attached EXP-006 to every new public case-study number and prohibited claims
  that the deterministic code and household candidates were autonomously
  invented by the model.
- Added a recursive evidence credential and local-path scan, manifest hash
  checks, remote-asset checks, all-node verification, registry reconstruction,
  and strict replay sentinels.
- Confirmed the README, evidence index, examples index, launch guide, changelog,
  and case-study index agree on commands, artifacts, costs, and limitations.
- Rechecked the frozen `pollard/v1` identity bytes against the golden vectors,
  canonical value rules, seven-method public `Store` protocol, and sync and
  async step-function contracts. No candidate freeze correction was required.
- Activated the documented deprecation policy and the four-surface 1.0
  covenant. Incompatible changes to those surfaces now require 2.0.

## 2026-07-13 v1.0.0 Local Release-Candidate Checkpoint

Scope:

- Added EXP-006A research synthesis, EXP-006B pinned code fix, and EXP-006C
  three-server MCP household recordings with inputs, SQLite trees, seals,
  content-free HTML, outcomes, a combined manifest, and offline verification.
- Completed the final reviewer-adversary pass, documentation audit, changelog
  audit, and four-surface API freeze review.
- Updated the package version and development classifier for 1.0.0.

Verification:

- Ruff passed for source, tests, examples, and the three recorded MCP servers.
- Mypy strict mode passed for 37 source files.
- Main suite: 238 passed and 6 PostgreSQL-only tests skipped without a DSN.
- Coverage: 91.24% against a 90% floor.
- Documentation writing scan and absolute-link scan passed. The built wheel
  metadata contains 14 Markdown links and every target is absolute HTTPS.
- The wheel and source distribution built successfully and passed Twine
  checks.
- A clean virtual environment installed the core wheel without dependencies,
  imported version 1.0.0, and exposed all nine CLI commands.
- The source distribution contains every EXP-006 input, recording, seal, HTML
  tree, outcome, manifest, case index, and recording or verification script.
- From the extracted source distribution, the core-only environment verified
  all 49 nodes and strictly replayed all six paths with no model call, tool
  execution, optional MCP dependency, or network use.

Cost, credentials, and publication status:

- No hosted-provider request occurred during EXP-006 or the 1.0 release gate.
  OpenAI, Anthropic, AWS, Azure, Google Cloud, and other provider spend was
  0 USD.
- No provider credential was read.
- At this checkpoint, 1.0.0 has not yet been pushed, merged, tagged, uploaded
  to PyPI, or published as a GitHub release.

## 2026-09-01 EXP-007 Local Per-Step Overhead Protocol

Status: registered; final installed-wheel measurement pending.

Question: what incremental per-step latency does Pollard add to a fixed local
callable for MemoryStore and SQLite record, hybrid-hit, and strict replay-hit
paths?

Protocol:

- Run 40 sequential steps per batch, discard two warmup batches, and retain 15
  measured batches for each backend and mode.
- Pair every Pollard batch with the same direct Python callable and alternate
  which timed region runs first. Disable garbage collection only inside each
  timed region.
- Start every record sample with an empty store and a fresh growing chain.
  Prepare matching hybrid and replay chains before timing, with sentinel model
  callables that fail if a recorded hit dispatches.
- Exclude runtime construction, fixture recording, store opening, validation,
  and cleanup from the timed regions. Retain every batch mean and summarize
  minimum, p50, p95, and maximum with no timing acceptance threshold.
- Pass only when the fixed workload result, callable counts, six case rows, and
  41-node tree shape satisfy the declared invariants.

Provenance gate:

- Build the candidate wheel from the final integrated source and install it in
  an isolated environment before measuring.
- Pass that exact wheel to `--package-wheel`. The runner refuses to continue if
  the wheel version or normalized Python-source digest differs from the loaded
  Pollard package.
- Also pass `--require-publishable-provenance`. The runner then requires the
  interpreter's PEP 610 archive SHA-256 to equal the supplied wheel, rejects
  editable installs, requires isolated imports from that installed
  distribution, requires the clean checkout's package-source digest to match
  the wheel and loaded package, and records `publishable: true` only with a
  known commit, a Git top level equal to the runner checkout, and `dirty: false`.
- Record the wheel SHA-256, installed-archive SHA-256, checkout and
  loaded-package digests, runner digest, repository commit, and repository dirty
  state.

No environment, timing, or performance finding is recorded at this checkpoint.
The raw result and numeric claims must be added only after the final integrated
wheel passes this provenance gate. No network or provider credential is needed.

## 2026-10-08 Rust PyPI 1.6.0 Parity: Protocol and Implementation

This is a separate native-port experiment, not the pending EXP-007 paired
Python-callable overhead protocol. The oracle is the published Pollard 1.6.0
wheel (SHA-256 569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f).
The original dirty Python checkout remains unchanged. Work was implemented in
an isolated worktree from 51e3a245641044fc8b3d6a90fc49f3cbfcbf107d.

Implementation covers runtime/registry semantics, async calls and streams,
streaming revalidation, measurements, Tokenmaster profiles, optional tokenization
and NVML sampling, all eight stores, shared reservations, seals/custody and
merge/import/export, MCP/OTel bridges and native operator commands. The evidence
README lists numeric limits, caller-owned SDK interfaces and database conditions.

Validation combines release-generated differential fixtures, native failure
regressions, stable and Rust 1.74 checks, optimized tests, package verification,
three SQLite interop programs, and live PostgreSQL/Redis/MongoDB/Neo4j/Kafka
interchange. Lost acknowledgements, reservation contention, cancellation,
post-result failures, corrupt state, reconnect and readonly boundaries are
covered where detailed in the test matrix. Remote suites are explicitly ignored
unless requested and fail when required configuration is absent. Reusable CI
jobs were added; no claim is made that hosted CI has already run.

A cross-language MongoDB test exposed the release's default naive datetime
conversion bug on non-UTC hosts. The mixed-runtime test uses Python
MongoStore(..., tz_aware=True); native server epoch time is not changed to
reproduce the incorrect local-time conversion. Real Neo4j routed discovery and
bookmarks are tested on one server, without claiming cluster failover coverage.

Performance protocol: release builds; two warmups/seven samples for identity,
MemoryStore record/hybrid/replay and wide traversal; separate fresh-database
SQLite40-step batches with WAL/NORMAL; three fresh-process stream samples at
10,000/200,000 chunks with unretained chunks and equal observers. The identical
MemoryStore benchmark runner also runs the archived original Rust core. Checksums,
accounting and no-dispatch invariants are checked outside timed regions.
Raw samples, medians, extrema, source/binary hashes and wheel import provenance
are retained. Process memory includes Python/native runtime startup. No real
provider latency, remote-store performance, pricing savings or energy savings
are inferred. No budgets are active in timing kernels, so budget-total cache
benefits are validated by read-count regressions rather than attributed to
these latency ratios. Provider spend is 0 USD.

Final review found a precision edge before measurement: the dependency's
ordinary Decimal parser could round tiny input prices/ledger amounts to zero.
A shared exact parser now rejects significant digits outside the native range,
checks original profile JSON before float conversion, bounds exponent expansion,
and permits only exact removal of redundant zeros. Cost multiplication/division
rejects nonzero underflow. Regression tests cover prices, limits, imported ledger
and recording charges, and Tokenmaster quotations. Finite nonzero decimal
arithmetic can still round; no arbitrary-precision claim is made.

Final native validation passed on unchanged sources: 185 default tests on stable
Rust1.94, Rust1.74 and optimized release builds; 205 optional-feature tests on
each toolchain; stable/MSRV default/optional Clippy with warnings denied;
formatting; verified package archive; all three SQLite interchange programs;
and explicit local NVML sampling. The 31 live service tests plus one Redis
configuration test, all five remote interchange programs and all five CLI
selectors passed after the decimal fix. The ten disposable-runner guards passed
on both Windows and Linux. All owned test containers were removed. Hosted CI
was configured but not executed here. Counts overlap across configurations.

## 2026-10-08 Rust PyPI 1.6.0 Parity: Earlier Checkpoint Measurements

Final release-build measurements completed with all workload checksum and
accounting assertions passing. Source and executable hashes remained unchanged
through each run. Raw samples, environment, protocols and validation receipts
are in evidence/rust-parity-1.6.0. This is separate from EXP-007.

- MemoryStore, 1,000-call chains: record 108.56x, strict replay
  453.43x, hybrid hits 2.45x faster than the pinned Python wheel.
  Against the original Rust core the respective ratios are
  46.26x, 99.12x and 56.96x.
- Wide traversal of 10,000 children: 9.28x faster than Python and
  86.56x faster than original Rust. The child index removes repeated
  whole-store child scans; immutable-prefix caching reduces repeated ancestry work.
- SQLite, 40 steps: record 1.39x and strict replay
  1.85x faster than Python, but hybrid takes
  2.48x as long. Native hybrid verifies ancestry; Python1.6 performs
  the explicit verification pass only for strict replay. External SQLite mutation
  prevents use of the native immutable-store cache. This is a behavior/cost
  difference; not all benchmarked paths have identical verification work.
- Identity-only hashing is 26.3% slower than original Rust, while still
  2.66x faster than Python. The experiment does not isolate the cost of
  expanded numeric and serialization fidelity from other changes.
- At 200,000 unretained chunks, native total peak process memory is
  5.20 MiB versus
  96.93 MiB for Python: 94.64% lower peak,
  with 1.76x faster callback/merge execution. This includes interpreter/runtime
  startup memory, not just chunk allocations. Three fresh processes per case.

Ratios summarize medians on one Intel i9-14900HX Windows host. Power profile and
OS background activity were not controlled. SQLite engines differ (Python3.43.1,
Rust3.45.0). Timing excludes providers, networking, setup and process startup;
process-memory measurements include startup. No energy, remote throughput,
provider-cost or complete-agent speedup claim is supported. Native budget-total
caching is supported by read-count/invalidation tests, not by these no-budget
latency workloads. Both regressions and raw distributions are retained.

## 2026-10-08 Rust 0.2.0 Completed Validation and Measurements

Source `be9e565a408776dd67f93891697503e8ae32c20a`; pinned Python 1.6.0 wheel;
Rust 1.94.0 release builds; Python 3.12.2; Intel i9-14900HX Windows 11 host.
The final matrix passes 212 default tests on stable/MSRV/optimized builds and 232
optional tests on each compiler (33 explicitly ignored, tested separately in
live-service/GPU receipts). Coherent SQLite snapshots fix the earlier CI
contention failure without inflating retry limits or caching historical reads.
The first 29 executed CI jobs passed. After macOS capacity retries, debug passed
but two optimized SQLite migration tests exposed colliding temporary paths.
The fixture correction and final CI confirmation remain in progress.
Current [remote CI evidence](evidence/rust-parity-1.6.0/release-ci-validation/remote/summary.json)
passes all 25 commands: 32 live tests plus one Redis configuration check, five
frozen-wheel interchanges, corrected-source MongoDB and five CLI selectors; all
five containers were removed. Fresh [default](evidence/rust-parity-1.6.0/release-ci-validation/consumer-default/summary.json)
and [all-feature](evidence/rust-parity-1.6.0/release-ci-validation/consumer-all/summary.json)
consumers resolved and ran on exactly Rust 1.74.0 with their own lockfiles.

The completed [release report](evidence/rust-parity-1.6.0/release-0.2.0.md)
contains all nine MemoryStore/identity rows, three SQLite rows and both stream
memory/time tables with medians and old/new ratios. Raw receipts are
[MemoryStore/identity](evidence/rust-parity-1.6.0/release-performance/performance.json),
[SQLite](evidence/rust-parity-1.6.0/release-performance/storage-performance.json) and
[stream memory/time](evidence/rust-parity-1.6.0/release-performance/stream-memory.json).
All source/executable guards passed; receipts preserve raw samples and hashes.
Documentation/evidence changes explain dirty-worktree flags without changing
measured source hashes.

- At 1,000 calls, Rust record/replay/hybrid medians are 30.869/11.731/18.406 ms,
  versus Python 5164.280/8062.100/71.702 ms: 167.30×/687.22×/3.90× ratios.
  Ratios versus original Rust 0.1.0 are 70.45×/145.42×/91.75×.
- Identity 10,000 median is 2.883 ms versus original Rust 11.679 ms and Python 40.092 ms:
  the earlier regression is resolved at 4.05× original Rust and 13.91× Python.
- SQLite 40-step record/hybrid/replay medians are 6.764/5.962/1.430 ms,
  versus Python 24.887/6.854/28.495 ms: 3.68×/1.15×/19.92×.
  Hybrid sample ranges overlap, and native ancestry checks are stronger.
- At 200,000 unretained chunks, median peak is 5.34 MiB versus Python 97.57 MiB
  (94.52% lower total process peak); callback/merge time is 62.846 ms versus
  114.144 ms (1.82×). At 10,000 chunks the corresponding memory reduction is 79.69%
  and time ratio 1.29×. Memory includes runtime/interpreter startup.

Methods: two warmups/seven samples for memory and SQLite kernels; three fresh
processes per stream case; equivalent successful outputs and accounting checked
outside timings. Child indexes, ancestry-prefix/revision caching, direct identity
encoding and allocation reduction explain intended benefits, without isolating
individual causes. SQLite snapshots retain correctness under mutation. Timing
kernels have no budgets; budget-cache benefits have read-count tests instead.
No provider, remote-throughput, energy, cost-saving or complete-agent speedup
claim follows. This remains separate from EXP-007. Python 1.6.0 MongoDB mixed
leases/windows require `tz_aware=True`; the source UTC fix is not a PyPI upload.
See the [tagged release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1) for final CI, source, archive and registry
receipts and publication status.

## 2026-10-08 Rust 0.2.1 Kafka Test-Fixture Follow-up

Rust 0.2.0 passed final pull-request CI and was published on crates.io; fresh
public-registry consumers and the installed CLI passed verification. After the
merge, main-branch CI exposed a 5 ms replay deadline shared by ordinary Kafka
mock tests. The Kafka change is confined to `cfg(test)`: ordinary mocks use
`KafkaOptions::new("audit")` and its existing 30-second default, while explicit
missing-record tests retain a 5 ms deadline. New regressions cover delayed
two-record replay and timeout rejection before producer creation. PostgreSQL
compares lease expiry with server time sampled immediately before releasing its
blocking transaction, preserving the 200 ms lease and detection of pre-lock
clock sampling. SQLite's renewal fixture starts with 60 seconds instead of one
second; its 1,000-second renewal and consistency assertions are unchanged.

Production timeouts, runtime code and benchmark programs are unchanged. The
0.2.0 measurements and their `be9e565` provenance remain historical evidence;
no new timing or optimization claim follows from this patch. Patch validation
and publication are separate from the completed 0.2.0 checks. See the
[Rust 0.2.1 release status and receipts](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1)
for final CI, source, archive and registry verification. npm 0.2.0 and the PyPI
1.6.0 release remain independent; the Python MongoDB source fix is not a new
PyPI upload.

## 2026-10-09 Python 1.6.1 Team Governance Validation

This release adds explicit team assignments, inherited budgets and tool
permissions, named quotas across task roots, result dependencies, durable
approval records, checkpoints, and per-agent reports. Team scheduling, message
delivery, authenticated identity, and external idempotency remain application
responsibilities. npm and Rust retain their independent release versions.

Four new offline examples exercise these paths with local functions:

- Example 15 records a planner, three specialists, and a reviewer. Its eight
  completed calls comprise five model calls and three tool calls, with 20
  synthetic tokens. Two refused probes make no dispatch. Strict replay returns
  the recorded results with zero model or tool dispatches.
- Example 16 gives four independent SQLite clients the same three-call limit.
  Three workers complete and one is refused. Serialized contexts and exact
  cursor checkpoints survive closing and reopening the store.
- Example 17 compares one worker with three on fixed local inputs. Both return
  the expected answer; the single worker uses one call and 23 synthetic tokens,
  while the team uses three calls and 33 synthetic tokens. This checks accounting
  and outcomes. It is not evidence of provider quality, cost, or speed benefits.
- Example 18 reconstructs a worker after restart, reads a retained approval,
  and performs one dummy external action using a stable operation ID.

Targeted tests cover conflicting scope configuration, concurrent reservations,
delegation and cursor tampering, inherited permissions, configuration drift,
approval reuse, stale confirmation tokens, and report attribution. The guide's
complete programs also run as tests, including the process-pool example.
The EXP-006 verifier reads all 49 retained nodes with no network access or live
dispatch. Release gates and public artifact receipts are recorded in the
[Python 1.6.1 release](https://github.com/jemsbhai/pollard/releases/tag/v1.6.1).
