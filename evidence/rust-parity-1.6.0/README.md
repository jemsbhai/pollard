# Rust parity with PyPI Pollard 1.6.0

Rust `pollardai` 0.2.0 targets the SHA-pinned Python `pollard==1.6.0` release.
The completed `be9e565` matrix passes **212 default tests** on stable/MSRV/release
and **232 optional-feature tests** on stable/MSRV, plus packaging, SQLite
interchange and NVML sampling. The current
[remote CI receipt](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-ci-validation/remote/summary.json)
covers 32 live tests, one Redis configuration check, all five frozen-wheel backend
interchanges, corrected-source MongoDB and all five CLI selectors. Fresh
[default](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-ci-validation/consumer-default/summary.json)
and [all-feature consumers](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-ci-validation/consumer-all/summary.json)
resolved and ran on exactly Rust 1.74.0 without borrowing the library lockfile.
The [release report](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-0.2.0.md) preserves the exact scope and source
provenance. CI passed its first 29 executed jobs. After macOS capacity retries,
debug passed but two optimized SQLite migration tests exposed colliding temporary
paths. The fixture correction and final CI confirmation remain in progress.
See the [tagged release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.0) for final CI, source, archive and registry
receipts and publication status.

The original Python 1.6.0 MongoDB store still requires **`tz_aware=True`** for
mixed-language windows and leases on non-UTC hosts. The repository's UTC fix is
separately tested and has not been uploaded to PyPI. Native Decimal and SDK/API
boundaries are detailed in the release report and below.

## Completed performance measurements

All three measurement sets completed against source `be9e565` with unchanged
source and executable hashes. Receipts record `git_dirty: true` because evidence
and documentation were being assembled; their source maps and unchanged-source
guards identify the measured code. Raw samples, extrema, medians, import-path
checks and hashes are retained in [MemoryStore/identity](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-performance/performance.json),
[SQLite](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-performance/storage-performance.json) and
[stream memory/time](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-performance/stream-memory.json).

Measurements used one Intel Core i9-14900HX Windows 11 host (32 logical CPUs),
Rust 1.94.0 release builds and Python 3.12.2. MemoryStore/identity and SQLite used
two warmups and seven retained samples, with deterministic engine-order shuffling.
SQLite used 40-step batches, a fresh database per sample, WAL/NORMAL and physically
read-only strict replay; Python SQLite was 3.43.1 and Rust's bundled SQLite 3.45.0.
Streaming used three fresh processes per case, equal counted observers,
`keep_chunks=false` and a constant final result. Windows peak working set includes
process/runtime startup. Timing excludes compilation, setup, process startup and
correctness checks. Checksum, accounting, callback-count and forbidden-dispatch
invariants passed. These are offline local kernels; power profile and background
OS activity were not controlled, and timing thresholds are not correctness gates.

**MemoryStore and identity: batch median times.** Ratios divide the comparison
median by Rust 0.2.0's median; values above one mean a shorter Rust time. Both Rust
versions use the same benchmark program. The baseline is the original 0.1.0 core
at `51e3a245641044fc8b3d6a90fc49f3cbfcbf107d`, with its source verified separately.

| Operation | Size | Python 1.6.0 ms | Rust 0.1.0 ms | Rust 0.2.0 ms | Python / Rust 0.2.0 | Old / new Rust |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| identity | 10,000 | 40.092 | 11.679 | 2.883 | 13.91× | 4.05× |
| record | 100 | 57.535 | 22.936 | 3.012 | 19.10× | 7.62× |
| record | 1,000 | 5164.280 | 2174.830 | 30.869 | 167.30× | 70.45× |
| replay | 100 | 84.689 | 17.384 | 1.153 | 73.43× | 15.07× |
| replay | 1,000 | 8062.100 | 1705.966 | 11.731 | 687.22× | 145.42× |
| hybrid | 100 | 7.472 | 17.381 | 1.789 | 4.18× | 9.72× |
| hybrid | 1,000 | 71.702 | 1688.737 | 18.406 | 3.90× | 91.75× |
| walk | 1,000 | 6.240 | 4.594 | 0.659 | 9.47× | 6.97× |
| walk | 10,000 | 70.142 | 636.744 | 8.030 | 8.74× | 79.30× |

**SQLite: batch median times.** The original Rust 0.1.0 core has no corresponding
SQLite implementation, so no old/new Rust ratio is reported here.

| Operation | Steps | Python ms | Rust ms | Python / Rust |
| --- | ---: | ---: | ---: | ---: |
| record | 40 | 24.887 | 6.764 | 3.68× |
| hybrid | 40 | 6.854 | 5.962 | 1.15× |
| replay | 40 | 28.495 | 1.430 | 19.92× |

**Unretained streams: median total process peak memory.**

| Chunks | Python peak MiB | Rust peak MiB | Lower total peak |
| --- | ---: | ---: | ---: |
| 10,000 | 25.61 | 5.20 | 79.69% |
| 200,000 | 97.57 | 5.34 | 94.52% |

**Unretained streams: median callback/merge execution time.**

| Chunks | Python ms | Rust ms | Python / Rust |
| --- | ---: | ---: | ---: |
| 10,000 | 4.229 | 3.275 | 1.29× |
| 200,000 | 114.144 | 62.846 | 1.82× |

The earlier identity regression is resolved: the current 10,000-identity batch
is 4.05× faster than the original Rust baseline and 13.91× faster than Python.
SQLite hybrid's median is now 1.15× faster than Python, but its sample ranges
overlap (Rust 5.450–6.484 ms; Python 6.094–7.568 ms), so this is a descriptive
median difference, not an established statistical advantage. Native hybrid also
verifies ancestry; Python 1.6.0 performs its explicit verification pass only for
strict replay. Successful outputs do not imply identical verification work.

The implementation reduces repeated work through indexed child traversal,
verified ancestry-prefix reuse and revision-aware SQLite caches. The snapshot
fallback preserves coherent verification during concurrent commits, without
promoting historical reads to the current cache. Direct identity encoding,
preallocated hexadecimal output and fewer temporary allocations reduce work in
the native path. Unretained stream chunks are released after merging, while the
Python release temporarily retains its list. These mechanisms explain intended
benefits, but the before/after measurements do not isolate each change's causal
contribution; added validation and metering also changed the implementation.
There are no budgets in the timing kernels: incremental accounting benefits are
supported by read-count and invalidation tests, not these latency ratios.

Total process memory includes interpreters, allocators and loaded libraries,
so the entire memory difference cannot be assigned to chunk retention. Results
are limited to these workloads and this host. They establish no provider-latency,
remote-throughput, energy-saving, provider-cost or complete-agent speedup claim.
They are separate from EXP-007's Python-callable overhead protocol. Earlier
checkpoint measurements remain historical and are superseded for this release.


The following audit and explicitly labelled earlier checkpoints remain available
for historical comparison; the completed results above are the current release measurements.

## Release and source provenance

- Oracle: [PyPI Pollard 1.6.0](https://pypi.org/project/pollard/1.6.0/), uploaded 2026-09-01, verified on 2026-10-08.

- Wheel SHA-256: `569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f`.

- Source archive SHA-256: `e9a43a52e6875add922d9749028f2c96e7e83dd138ff65ef4d3f834485617fec`.

- Original Rust comparison source: commit `51e3a245641044fc8b3d6a90fc49f3cbfcbf107d`, crate 0.1.0.

- The original dirty Python checkout was left untouched; implementation and validation used a separate managed Git worktree.

- Final validation and benchmark JSON files include source SHA-256 maps, commands, toolchain versions and/or executable hashes. Benchmark workers verify the extracted wheel's contents and imported module path.

`api-inventory.json`, `parity-checklist.json` and `behavior-fixtures.json` are the initial release audit. The checklist describes **baseline gaps**, not remaining gaps. Earlier `storage-validation.json`, `storage-source-sha256.json` and `validation-release.log` are intermediate checkpoints, superseded by the final matrix. The 0.2.0 release receipt identifies the current matrix and backend evidence; the earlier checkpoint remains available for comparison.

Top-level backend validation files and the Linux Kafka TLS log are component checkpoints with their own provenance. The final `final-validation/validation.json` and `remote-validation/summary.json` bind the combined native/live checks to the completed source. Formatting and the final decimal fix happened after some earlier component snapshots.

The tables and `final-validation` receipts below describe the earlier checkpoint.
The [0.2.0 release receipt](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/release-0.2.0.md) supersedes its test counts, dependency
constraints and timing results after the final compatibility fixes.

## Implemented compatibility

| Area | Native implementation and evidence |

| --- | --- |

| Identity | Frozen identity/result/redaction/seal domains; arbitrary-size identity integers; Python floating-point result spelling; original imported result bytes; Unicode and boundary fixtures |

| Registry | Ordered registry digests, local schema references, sensitive commitments, fail-closed unsupported validation, policies, confirmation, sync and async handlers |

| Runtime | Record duplicates preserve first results and collect conflicts; optional early refusal; replay/hybrid; branches, rollback, resume, prune, reports, dry run and observers |

| Accounting | Default meters, Decimal prices and budget comparisons, usage fallback, custom meters, window ledgers, shared reserve/renew/settle/release and ambiguous-outcome handling |

| Async and streams | Governed async calls and tools, incremental chunk merging, retained replay, observer errors, cancellation and panic cleanup; live streaming revalidation with separate observations |

| Measurements | Start/finish/readings lifecycle with reverse cleanup, power/counter energy integration, explicit optional NVML adapter, diagnostic cleanup errors |

| Tokenmaster | Explicit offline profile registry, context/output limits, aliases/calibration, pricing tiers, cache/reasoning usage, gauge/advice and governance meters; pinned 0.2.0 differential fixtures |

| Token estimates | Optional embedded OpenAI-family BPE; 144 release-oracle cases and overflow failures |

| Stores | Memory, SQLite, HashRope, PostgreSQL, Redis, MongoDB, Neo4j and Kafka |

| Audit | Exact seals and custody, export/import, validated merge, conflicts, pruning/GC, explicit transactional legacy SQLite migration |

| Providers | OpenAI Responses/Chat, Anthropic, Bedrock and LiteLLM result and incremental stream normalization |

| Bridges | Caller-owned MCP discovery/async calls and content-free OTel exporter with parent/cleanup behavior |

| CLI | All nine operator commands, safe remote environment references, read-only inspection, ASCII/Unicode and escaped HTML rendering, explicit destination creation |

SQLite and PostgreSQL use their Python schemas directly. Redis, MongoDB and Neo4j share the release's transactional KV records and lease/tombstone format. Kafka uses the canonical event stream, operation identities and reconnect validation. HashRope matches append bytes and its base-131, modulus `2^61-1` polynomial hash.

## Compatibility and validation limits

Native equivalents do not instantiate Python decorators, Pydantic objects, pytest plugins or Python framework SDK classes. Applications supply JSON schemas, Rust tests, normalized provider callbacks and MCP/OTel transport traits. Tokenmaster profiles are supplied explicitly; there is no implicit catalog refresh. CLI output defaults to JSON and HTML presentation is native.

Attempts are `u64`, exact step/token accounting uses the safe integer range, and `rust_decimal` has a finite 96-bit coefficient with scale up to 28. In 0.2.0, unsupported decimal input precision and unrepresentable accounting results are rejected without rounding significant digits. Native coefficient/scale bounds and public f64 interfaces still differ from Python's wider exponent range/configurable Decimal context. Merge preparation retains validated source data in memory instead of Python's disk spool. Each SQLite import/merge is transactional, but a multi-source CLI merge is not globally atomic; append-only backends cannot undo acknowledged events. Runtime handles are single-thread `Rc`; independent persistent-store runtimes coordinate through the database. These are meaningful language/API differences, not a claim of byte-identical exceptions or arbitrary Python object compatibility.

For mixed Python/Rust MongoDB windows and leases, the original Python 1.6.0 wheel must use **`tz_aware=True`**. Its default PyMongo naive UTC datetime is converted with local `.timestamp()` semantics: the recorded non-UTC test host observed an approximately six-hour clock error. Rust uses server epoch milliseconds. The frozen-wheel interoperability checks use the timezone-aware Python configuration and retain evidence of the upstream default behavior. The 0.2.0 source update also fixes Python's UTC conversion; a separately identified companion check exercises corrected defaults without altering the frozen wheel oracle.

The native Decimal type retains only nonnegative scale. Version 0.2.0 adds `reserve_decimal_text` and `settle_decimal_text` to preserve Python Decimal scale/exponent and signed-zero spellings in reservation/settlement fingerprints. Use these methods when retrying manually assigned reservation IDs across languages; numeric Decimal APIs cannot recover spelling already lost by conversion. Ordinary runtime-generated integer/float forms and the scientific forms exercised by the interop suites match.

Remote tests use dedicated local standalone services (MongoDB is a single-node replica set); they do not establish multi-node failover, partitions, availability or throughput. Neo4j routed URI/bookmark behavior is exercised against one server. Kafka has no shared budget arbiter and requires a dedicated pre-existing single partition with unlimited retention. Its transient pending marker can be lost on a process crash. Linux Kafka TLS/SASL capability tests do not test live certificate authentication; GSSAPI is not enabled. NVML was exercised on one NVIDIA GPU and reports device-wide energy, not a measured optimization benefit.

## Final validation results

The completed-source matrix passed with unchanged source hashes. See

[`final-validation/validation.json`](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/final-validation/validation.json),

[`remote-validation/summary.json`](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/remote-validation/summary.json) and the

[remote interpretation](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/remote-validation/interpretation.json).

| Check | Result |

| --- | --- |

| Default tests, stable Rust 1.94 | 185 passed |

| Default tests, Rust 1.74 | 185 passed |

| Optimized release tests | 185 passed |

| Optional features, stable and Rust 1.74 | 205 passed on each; 32 explicitly ignored |

| Live backends after the final decimal fix | 31 passed, plus 1 Redis configuration check |

| Live remote interoperability | PostgreSQL, Redis, MongoDB, Neo4j, Kafka passed |

| Remote CLI selectors | All 5 passed; recorded trees unchanged |

| SQLite interoperability | Recording/seal/custody, direct arbitration and mixed runtime programs passed |

| Local NVIDIA hardware | 1 explicit NVML sampling test passed |

| Formatting and warnings-denied Clippy | Default/optional stable and Rust 1.74 passed |

| Crate package | Archive built and verified successfully |

| Disposable-service runner guards | 10 passed on Windows and 10 on Linux |

The optional suite includes the default tests; these counts must not be added

as unique cases. The 32 ordinary-run ignores are the 31 live service tests and

1 GPU test, exercised separately above. Differential fixture loops cover many

additional oracle cases inside those test functions. GitHub Actions has been

configured for cross-platform core checks, optional Linux TLS builds and live

services, but hosted CI has not run in this work session.

## Reproduce correctness validation

Download the wheel without dependencies and retain it outside the source package:

```sh

python -m pip download --no-deps --only-binary=:all: pollard==1.6.0 --dest .benchmarks/pypi-oracle

python evidence/rust-parity-1.6.0/validate_native.py --wheel .benchmarks/pypi-oracle/pollard-1.6.0-py3-none-any.whl

```

The runner executes formatting, warnings-denied Clippy, stable and Rust 1.74 default/optional tests, optimized tests, package verification and all three SQLite interop programs. Add `--nvml` only on a machine with the NVIDIA runtime. On Windows, the optional matrix excludes `kafka-tls` because vendored OpenSSL needs additional build prerequisites. `kafka_tls_build.sh` and its Linux Rust 1.74 log cover TLS compilation and driver capabilities. The workflow adds Linux feature and live-service jobs; adding a workflow is not evidence that hosted CI has already run.

Live database tests are explicitly ignored during ordinary `cargo test`. Requesting them without required environment values fails; they are never silently counted as successful live tests. The portable remote runner creates labelled isolated Docker services, uses the SHA-pinned wheel and pinned Python drivers, runs native and cross-language checks, saves command logs and removes only its own containers. Consult its `--help` for service selection and output paths.

```sh

python evidence/rust-parity-1.6.0/validate_remote.py --wheel .benchmarks/pypi-oracle/pollard-1.6.0-py3-none-any.whl

```

## Performance protocol

These are separate native-port experiments, **not EXP-007's pending paired direct-Python-callable overhead experiment**. No EXP-007 publication/provenance claim is inferred. Measurements compare successful equivalent deterministic workloads, with checksums, accounting and forbidden-dispatch assertions outside timed regions.

- Release build on one Windows machine, Intel Core i9-14900HX. JSON records compiler, Python, OS and source/binary hashes.

- Memory kernels: two warmup batches, seven retained samples; identity (10,000), record/hybrid/replay chains (100 and 1,000), wide traversal (1,000 and 10,000). Engine order is deterministically shuffled, but samples within each engine are sequential.

- SQLite: 40 steps, fresh database per sample, two warmups and seven samples; WAL/NORMAL for both implementations; physically read-only strict replay. SQLite engine versions are recorded and may differ.

- Streaming: three fresh processes at 10,000 and 200,000 chunks, `keep_chunks=false`, equal counted observers and a constant final result. Windows reports total peak working set, including interpreter/runtime startup. This is not allocator-only retained memory.

- Local callbacks only: no network/provider work, no credentials or hosted spend. Timing excludes compilation, process startup, fixture setup and correctness checks. Memory includes process startup.

- No timing threshold is a correctness gate. Report medians, raw samples, min/max and regressions; ratios are Python/updated Rust or original Rust/updated Rust.

Both Rust versions run an identical `performance.rs`. The baseline core is checked against `git show` of the pinned commit. The old implementation lacks several added validation and metering features, so the old/new comparison is a before/after application measurement, not an isolated optimization ablation. Memory kernels have no budgets: they **do not quantify budget-total caching**.

Reproduction (the first command restores the pinned baseline and identical example):

```sh

python evidence/rust-parity-1.6.0/prepare_baseline.py

cargo build --release --locked --examples --manifest-path crates/pollardai/Cargo.toml

cargo build --release --locked --example performance --manifest-path .benchmarks/rust-baseline/crates/pollardai/Cargo.toml

python evidence/rust-parity-1.6.0/benchmark.py

python evidence/rust-parity-1.6.0/storage_performance.py

python evidence/rust-parity-1.6.0/stream_memory.py

```

`stream_memory.py` requires `psutil`. Stop other builds and dedicated test services first. All runners reject changed sources/binaries during measurement and stale release binaries. Source/binary hashes identify the measured state; freshness timestamps are not cryptographic proof of compilation.

## Earlier checkpoint performance results

All values below are **batch medians on this one host**. Speedup is comparison time divided by updated Rust time; below 1 means Rust took longer. Raw samples and hashes are in [performance.json](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/performance.json), [storage-performance.json](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/storage-performance.json), and [stream-memory.json](https://github.com/jemsbhai/pollard/blob/main/evidence/rust-parity-1.6.0/stream-memory.json).

### MemoryStore and identity

| Operation | Size | Python ms | Original Rust ms | Updated Rust ms | Python / Rust | Old / new Rust |

| --- | ---: | ---: | ---: | ---: | ---: | ---: |

| identity | 10,000 | 39.392 | 11.742 | 14.825 | 2.66× | 0.79× |

| record | 100 | 54.364 | 21.388 | 4.446 | 12.23× | 4.81× |

| record | 1,000 | 5072.514 | 2161.510 | 46.726 | 108.56× | 46.26× |

| replay | 100 | 79.474 | 16.623 | 1.535 | 51.78× | 10.83× |

| replay | 1,000 | 7626.224 | 1667.146 | 16.819 | 453.43× | 99.12× |

| hybrid | 100 | 7.293 | 16.839 | 2.857 | 2.55× | 5.89× |

| hybrid | 1,000 | 71.738 | 1669.686 | 29.313 | 2.45× | 56.96× |

| walk | 1,000 | 6.301 | 4.561 | 0.589 | 10.69× | 7.74× |

| walk | 10,000 | 67.899 | 633.103 | 7.314 | 9.28× | 86.56× |

### SQLite

| Operation | Steps | Python ms | Rust ms | Python / Rust |

| --- | ---: | ---: | ---: | ---: |

| record | 40 | 24.888 | 17.841 | 1.39× |

| hybrid | 40 | 7.456 | 18.467 | 0.40× |

| replay | 40 | 30.021 | 16.201 | 1.85× |

Python SQLite is 3.43.1; native bundled SQLite is 3.45.0.

### Unretained streams

| Chunks | Python peak MiB | Rust peak MiB | Lower total peak | Time speedup |

| --- | ---: | ---: | ---: | ---: |

| 10,000 | 25.55 | 5.61 | 78.03% | 1.23× |

| 200,000 | 96.93 | 5.20 | 94.64% | 1.76× |

### Regressions and interpretation

- Identity-only hashing took **26.3% longer than the original Rust core**, while remaining 2.66× faster than Python. Expanded numeric/serialization fidelity accompanies this change; the experiment does not isolate its causal cost.

- SQLite hybrid hits took **2.48× as long as Python** (147.7% longer). The native runtime verifies ancestry on hybrid hits; Python 1.6.0's `recorded_node_or_missing` invokes its explicit verification pass only for strict replay. SQLite cannot use the native immutable-store cache, so this stronger check repeats database reads. This is a substantive behavior/cost difference, not evidence that every native path is faster.

- MemoryStore hybrid speedup is much smaller than strict replay speedup for the same reason: Python's hybrid and strict-replay paths do different verification work. The unchanged successful outputs do not imply identical verification costs.

- The original native implementation also verified hybrid ancestry. Its large old/new hybrid improvement supports the cache benefit for that native path; it does not justify weakening verification on externally mutable stores.

- These kernels do not measure hosted model latency, remote-store throughput, concurrency, startup latency, energy savings or provider costs. The largest ratios apply to growing in-memory chains, not complete agent workloads.

## Earlier checkpoint optimization notes

MemoryStore's child index replaces repeated full-map scans during traversal. The runtime verifies an immutable ancestry prefix once and extends it as calls advance; custom or externally mutable stores retain full verification unless they explicitly guarantee revision/identity invariants. Incremental charge totals invalidate on revision changes and account for sibling work; read-count and mutation regressions validate this separately from latency benchmarks.

Unretained stream chunks are released after merging, whereas the Python 1.6.0 consumer temporarily retains its chunk list. Total process peak also includes the interpreter, allocators and loaded libraries, so the entire cross-language memory difference cannot be attributed to that change. SQLite, remote database costs and provider latency remain separate from MemoryStore gains.

