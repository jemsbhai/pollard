# Findings

Each finding cites a logbook entry. Do not copy a number from this file unless
the linked logbook entry supports the same scope.

## 2026-07-13 Phase 4 Offline Pass

Source: `LOGBOOK.md`, entry `2026-07-13 EXP-001, EXP-002, EXP-003 Result`.

Scope: deterministic mock runs only. Local model, wall-clock, dollar, and joule
metrics were not measured.

Findings:

- EXP-001: for a shared-prefix tree, measured mock token spend matched `p+n*s`
  with max prediction error 0.0 percent across branch counts 2, 4, and 8.
- EXP-001: mean mock-token savings were 45.352634 percent for 2 branches,
  68.028951 percent for 4 branches, and 79.367109 percent for 8 branches.
- EXP-002: token budget refusal fired before the third call; overshoot was 3
  tokens against a one-settle bound of 4 tokens.
- EXP-003: the unregistered hostile tool request did not execute and recorded a
  policy refusal with the registry digest.

## 2026-07-13 Phase 8 Scale-Out Checkpoint

Source: `LOGBOOK.md`, entries `2026-07-13 EXP-005 Draft Plan` and
`2026-07-13 v0.8.0 Scale-Out Acceptance Checkpoint`.

Scope: local Docker PostgreSQL 16 and SQLite acceptance tests. This is not a
network-latency or throughput benchmark.

Findings:

- In 20 repeated rounds, two operating-system processes sharing one
  PostgreSQL logical store executed exactly four calls under
  `Budget(steps=4)`.
- Two-thread request-window contention executed exactly three calls under a
  three-request window on both SQLite and PostgreSQL.
- A 1,001-node SQLite merge remained verify-clean and preserved every
  rehydrated payload's canonical bytes.
- No model-provider or cloud API request was made and provider spend was 0 USD.

## 2026-07-13 Phase 9 Formal Evidence Pass

Source: `LOGBOOK.md`, entries `2026-07-13 EXP-001 Local-Model Result`,
`2026-07-13 EXP-004 Formal Storage-Curve Result`, and
`2026-07-13 EXP-005 Formal Contention Result`.

Scope: one RTX 4090 Laptop GPU local inference environment, deterministic local
SQLite storage workloads, and same-host Docker PostgreSQL 14 and 18 contention.
No hosted model was called.

Findings:

- EXP-001: output digests matched between naive and shared-prefix conditions in
  every seed. Mean wall-clock savings were 40.052576%, 59.134399%, and
  68.539428% at 2, 4, and 8 branches, with 95% confidence-interval half-widths
  of 6.179807, 1.276648, and 1.232860 percentage points.
- EXP-001: raw whole-GPU NVML energy savings were 35.227991%, 58.534159%, and
  67.584319%, with 95% confidence-interval half-widths of 10.428756, 2.936724,
  and 1.459938 percentage points. The USD conversion uses the declared
  0.20 USD/kWh comparison rate and is not actual utility cost or total cost of
  ownership.
- EXP-004: at 200 turns, mean closed-database size was 4,255,744 bytes with
  interning and 165,683,200 bytes without it, a 38.931665 ratio. The fitted
  finite-range log-log exponents were 1.201388 and 1.970694. This does not prove
  an asymptotic complexity class.
- EXP-005: all 1,650 rounds passed across PostgreSQL 14 and 18. Exact step and
  request conditions never exceeded their limit. The maximum observed
  estimated-token overshoot was 6 tokens and every round stayed within the
  actual-minus-estimate bound.
- EXP-005: intentionally abandoned reservations returned active capacity on
  the first precheck after expiry at 1, 2, and 4 second leases in every
  registered seed.
- Provider spend was 0 USD. These results support only the recorded scopes and
  do not support hosted-provider savings, general throughput, availability,
  consensus, or total-cost claims.

## 2026-07-13 EXP-006 End-to-End Case Studies

Source: `LOGBOOK.md`, entries `2026-07-13 EXP-006 End-to-End Case-Study
Protocol`, `2026-07-13 EXP-006 End-to-End Case-Study Result`, and
`2026-07-13 Phase 9 Final Reviewer-Adversary and API Freeze Review`.

Scope: three local adapter and tool workflows recorded with pinned
Qwen2.5-Coder 7B and llama.cpp files. Recording used loopback HTTP and local
stdio only. Verification and replay use no network.

Findings:

- EXP-006A rejected incomplete source coverage and selected a model-generated
  synthesis that passed its registered document, citation, and phrase checks.
- EXP-006B rejected a candidate that failed reversed-bounds handling and
  selected a candidate that passed all four pinned tests.
- EXP-006C rejected a 2,897-cent order against a 2,000-cent limit and selected
  a 1,547-cent order through three actual local MCP stdio servers.
- All 49 nodes verified, all three subtree seals matched, and all six
  root-to-leaf paths replayed strictly with no model or tool function
  execution.
- No hosted model call occurred and provider spend was 0 USD.

Interpretation boundary:

- The research synthesis is model-generated. The code-fix and household
  candidates are deterministic controller inputs reviewed by the model. Those
  cases support governance and workflow claims, not autonomous-invention
  claims.
- Offline replay establishes integrity and availability of the committed
  semantic results. It does not establish deterministic regeneration by the
  local model.

## 2026-09-01 EXP-007 Protocol Status

Source: `LOGBOOK.md`, entry `2026-09-01 EXP-007 Local Per-Step Overhead
Protocol`.

No quantitative finding is registered yet. The final run must load the exact
candidate wheel, prove that its normalized Python sources match the loaded
package and clean checkout, record the wheel and runner SHA-256 digests, identify
a clean repository commit, and report `publishable: true`. Timing values do not
determine pass or fail.

Any later result is descriptive for its recorded local environment. It cannot
establish a provider, network, concurrency, remote-store, throughput, service
level, or performance-guarantee claim.

## 2026-10-08 Native Rust Parity and Performance: Earlier Checkpoint

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
See the [tagged release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.0) for final CI, source, archive and registry
receipts and publication status.
