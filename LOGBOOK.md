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
