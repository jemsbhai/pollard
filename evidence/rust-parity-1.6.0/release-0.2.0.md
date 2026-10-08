# Rust pollardai 0.2.0

## Follow-up: Rust 0.2.1 test-fixture correction

Rust 0.2.0 passed its final pull-request CI and was published on crates.io;
fresh public-registry consumers and the installed CLI passed verification.
A later main-branch CI run exposed an overly short 5 ms replay deadline shared
by ordinary Kafka mock tests. The Kafka correction is confined to `cfg(test)`:
ordinary mocks use `KafkaOptions::new("audit")` and its existing 30-second
default, while explicit missing-record cases retain a 5 ms deadline. New
regressions exercise delayed two-record replay and rejection of missing records
before producer creation. Two integration fixtures also avoid scheduler-sensitive
assumptions: PostgreSQL compares stored expiry against server time captured
immediately before releasing its blocking transaction, retaining the 200 ms
lease and detection of pre-lock clock sampling; SQLite's renewal fixture starts
with 60 seconds instead of one second and retains its 1,000-second renewal and
consistency assertions. These changes do not alter production timeouts, runtime
behavior or benchmark code. Patch validation and publication remain separate
from the completed 0.2.0 results below. See the
[Rust 0.2.1 release status and receipts](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1)
for final CI, source, archive and registry verification.

## Rust 0.2.0 evidence and provenance

The completed local validation matrix for Rust 0.2.0 used
source `be9e565`: 212 default tests and 232 optional-feature tests pass on both
stable Rust 1.94 and Rust 1.74, and all 212 default tests pass in the optimized
build. Formatting, warnings-denied Clippy, package verification, SQLite
interoperability and explicit NVML sampling also pass with unchanged source
hashes. Later macOS CI exposed a collision in timestamp-only temporary test
paths. The helpers now include atomic serials, and an additional regression
forces eight threads to allocate 1,024 paths at an identical clock tick.
Migration tests pass in debug and release builds on Rust 1.74 and 1.99,
including 20 repeated runs per compiler/profile. These subsequent changes affect
test fixtures only; the production and benchmark sources remain unchanged.
See the [tagged release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1)
for final CI, source, archive and registry receipts and publication status.
Publication scope is the Rust crate; the Python release oracle remains 1.6.0.

The target is the published Python 1.6.0 wheel, SHA-256
`569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f`.
The tests load that verified artifact independently of the editable Python
source. The [release audit](api-inventory.json) and
[behavior fixtures](behavior-fixtures.json) identify the source contracts;
the initial [checklist](parity-checklist.json) records baseline gaps rather than
remaining work.

The Rust implementation now covers identity and result encoding, registry
digests and policies, record/replay/hybrid execution, branching and revalidation,
sync/async calls and incremental streams, custom meters and shared budget
reservations, measurement cleanup, and explicit cancellation/unknown-outcome
handling. Storage includes Memory, SQLite, HashRope, PostgreSQL, Redis, MongoDB,
Neo4j and Kafka, with seals, custody, export/import, validated merge, conflicts,
and applicable migration and garbage-collection operations. Native provider
normalizers, Tokenmaster profiles and meters, optional token estimation and
NVML, MCP/OTel bridges, rendering and operator commands are included. The
[crate API guide](../../crates/pollardai/README.md) describes the interfaces and
feature flags.

SQLite and PostgreSQL use the corresponding Python schemas; the transactional
KV stores share record, reservation, lease and settlement formats. HashRope
append bytes and hashes and Kafka event envelopes have differential fixtures.
Later compatibility fixes add exact Decimal input and arithmetic rejection,
Python-compatible cost scale and reservation-text fingerprints, and SQLite
cache invalidation across independent writers, schema changes and ambiguous
commits. See the [Decimal tests](../../crates/pollardai/tests/decimal_exact.rs),
[wire fixtures](../../crates/pollardai/tests/pypi160_decimal_wire.json) and
[SQLite cache tests](../../crates/pollardai/tests/sqlite_cache.rs).

Hosted CI previously reproduced a SQLite concurrency failure across all platform
jobs at source `8176e3a`: eight workers could exhaust three optimistic attempts
while other workers committed legitimate changes. The SQLite contention fix
keeps the existing three optimistic attempts and
then requests a consistent backend snapshot. SQLite reads the complete ancestry
or subtree, including external ancestors and interned payload blobs, inside one
deferred read transaction. Verification, accounting and depth checks validate
the detached records; transaction cleanup also covers failures, and a rejected
nested transaction preserves the caller's transaction. This addresses the
concurrent-read race without increasing the retry limit. Snapshot results are
returned uncached: historical data is never assigned to a newer live revision,
and subsequent reads observe external commits. Backends without snapshot support
continue to fail closed under sustained changes. The implementation is in
[SQLite](../../crates/pollardai/src/sqlite.rs), the
[runtime](../../crates/pollardai/src/runtime.rs) and the
[recording-store contract](../../crates/pollardai/src/store.rs).

Completed targeted checks for this new fix are 84 tests on each of Rust 1.74.0
and 1.99.0: 26 runtime, 17 cache, 13 arbitration, 22 storage, five cleanup and
one transaction/blob-coherence unit test. Both toolchains pass Clippy with
warnings denied and formatting checks. Each also passes 20 repeated contention
runs, each with eight workers in both budget and window scenarios
([Rust 1.74](release-contention-validation/stress-rust174.json),
[Rust 1.99](release-contention-validation/stress-rust199.json)). These are
targeted results that supplement the completed full matrix below. The cache tests
cover later external commits becoming visible, tampering and missing ancestors,
and fallback without historical cache promotion.

The primary release matrix used
[`be9e565a408776dd67f93891697503e8ae32c20a`](https://github.com/jemsbhai/pollard/commit/be9e565a408776dd67f93891697503e8ae32c20a),
including the snapshot fallback. Its [receipt](release-final-validation/validation.json)
records `all_checks_passed: true` and `source_unchanged_during_run: true`.
Optional tests include default tests, and repeated toolchains execute the same
cases; these counts must not be added as unique coverage.

| Completed check at `be9e565` | Completed result | Receipt |
| --- | --- | --- |
| Default tests | 212 passed on stable Rust 1.94, 212 on Rust 1.74, and 212 in the optimized build | [Native matrix](release-final-validation/validation.json) |
| Optional features | 232 passed on stable and 232 on Rust 1.74; 33 explicitly ignored in each ordinary run | [Native matrix](release-final-validation/validation.json) |
| NVIDIA hardware | One explicit device-wide NVML sampling test passed | [Hardware log](release-final-validation/nvml-hardware.log) |
| SQLite interchange | Recording/seals/custody, direct arbitration and mixed runtime budget/window checks passed | [Storage](release-final-validation/interop-sqlite.log), [arbitration](release-final-validation/interop-arbitration.log), [runtime](release-final-validation/interop-runtime.log) |
| Formatting, lint and packaging | Stable/MSRV formatting, warnings-denied Clippy and crate package verification passed | [Native matrix](release-final-validation/validation.json), [package log](release-final-validation/package.log) |
| Live services and remote interchange | 32 live tests plus one Redis configuration check; all 25 commands passed, five frozen-wheel backend interchanges, corrected-source MongoDB and five CLI selectors passed | [CI remote receipt](release-ci-validation/remote/summary.json) |
| Fresh Rust 1.74.0 consumers | Default and all features, including TLS, resolved without borrowing the library lockfile and compiled and ran successfully | [Default](release-ci-validation/consumer-default/summary.json), [all features](release-ci-validation/consumer-all/summary.json) |

The 33 optional-test ignores are the 32 live service tests and the GPU test,
which are counted only in their separately executed receipts. The Windows
optional matrix excludes vendored Kafka TLS. The current CI consumer receipts
above use exactly Rust 1.74.0 for both default and all features, including TLS.
Each consumer generated its own lockfile instead of copying the library
lockfile. Earlier local consumer checks exposed Rust 1.74 resolution failures that
locked repository builds had hidden.
The [manifest](../../crates/pollardai/Cargo.toml) constrains incompatible
transitive versions, and the [CI workflow](../../.github/workflows/native.yml)
now repeats clean default/all-feature consumer resolution.

The current [remote CI run](release-ci-validation/remote/summary.json) used
`be9e565`, records `source_changed_during_run: false`, and passed all 25 commands.
[Redis interchange](release-ci-validation/remote/results/redis-interop.json)
includes 11 bidirectional Decimal-text cases; the
[CLI receipt](release-ci-validation/remote/results/cli-remote-inspection.json)
checks all five read-only remote selectors. Its receipt records service images,
commands, source hashes and executable hashes. All five uniquely labelled test
containers were removed successfully. CI artifact files retain their raw bytes.

The historical [206-default/226-optional matrix](release-validation/validation.json)
and [earlier local remote run](release-remote-validation/summary.json) used
[`15e771aaca2d3e4b4b3fbe8aa833e121a5c5db3b`](https://github.com/jemsbhai/pollard/commit/15e771aaca2d3e4b4b3fbe8aa833e121a5c5db3b).
Both receipts include source hashes and confirm unchanged source during their
runs. These remain checkpoint evidence, superseded by the current matrix and
remote CI receipts above. The interrupted
[earlier remote checkpoint](release-remote-checkpoint/summary.json) deliberately
retains its failed source-change guard.

[Hash provenance](release-validation/hash-provenance.json) preserves the original
receipt hashes and distinguishes CRLF-to-LF source normalization from actual
post-validation source changes. Evidence `.log` and `.json` artifacts are stored
with Git text conversion disabled so their raw bytes match the recorded hashes.
This mapping explains artifact preservation; it does not claim that later code
was covered by an earlier test run.

The subsequent source at
[`8176e3a3277039fcfcac9d1d665ec802a7118697`](https://github.com/jemsbhai/pollard/commit/8176e3a3277039fcfcac9d1d665ec802a7118697)
changes two equivalent expressions for current stable Clippy: object-key sorting
and appending Kafka's newline byte. It also supplies the Linux TLS build
prerequisite and updates evidence/documentation. Current stable
[Clippy passed](release-validation/clippy-rust199.log), and all ten
[targeted Kafka tests passed](release-validation/kafka-post-ci-fix.log).
The earlier full-matrix receipt is not relabelled as a full rerun of this later
commit. Historical fresh-consumer receipts retain their own source hashes;
the current CI consumer receipts above use `be9e565`. Hosted CI at `8176e3a`
exposed the concurrent SQLite
failure described above. The snapshot correction now has the completed
`be9e565` full matrix and repeated contention checks. The later macOS fixture
failure is corrected by per-process atomic serials in the migration, identity,
runtime, Kafka and Redis test helpers. The migration regression deliberately
uses identical timestamps across threads; both compilers pass all four tests
in debug and release, plus 80 repeated test-binary runs overall
([Rust 1.74](release-fixture-validation/migration-stress-rust174.json),
[Rust 1.99](release-fixture-validation/migration-stress-rust199.json)). The final
suite included this one additional test beyond the local matrix above. The
subsequent 0.2.0 pull-request CI and registry checks passed; the separate Kafka
test-fixture issue found after merging is described in the follow-up above.

MongoDB needs an explicit distinction between the release oracle and the source
fix. The unchanged Python 1.6.0 wheel must use **`tz_aware=True`** for mixed
Python/Rust leases and windows on non-UTC hosts. Its default BSON UTC datetime
is naive, and calling `.timestamp()` interprets it as local time; the
[frozen-wheel check](release-remote-validation/results/mongodb-interop.json)
records the resulting approximately six-hour error on this host. Rust already
uses the server UTC epoch. The [Python source fix](../../src/pollard/stores/mongodb.py)
normalizes naive BSON datetimes to UTC and preserves explicit offsets, without
emulating the erroneous clock. That Python fix has **not been uploaded to
PyPI**. A separately identified [corrected-source live check](release-ci-validation/remote/results/mongodb-corrected-source-interop.json)
passes with default client options, including shared-budget contention and
settlement. [Timezone regressions](release-consumer-validation/checkpoint.json)
passed all 20 Linux cases, including UTC, Mountain and India settings; the
Windows targeted group passed 159 tests with two POSIX-only skips.

There remain deliberate language and representation boundaries. Native Decimal
amounts have a 96-bit coefficient and scale up to 28; unsupported input precision
and unrepresentable arithmetic results fail rather than silently rounding
significant digits. This does not reproduce Python's wider exponent range or
configurable Decimal context. `reserve_decimal_text` and `settle_decimal_text`
preserve representable Python spellings such as `1E+2`, `-0.00` and `1.2300`
for cross-language retries; numeric APIs cannot recover spelling discarded by
the caller. Public `f64` interfaces retain floating-point limits, attempts are
`u64`, and exact step/token accounting uses the safe integer range.

Python decorators, Pydantic introspection, pytest hooks and Python SDK objects
are represented by Rust schemas, tests, normalized callbacks and transport
traits. Applications own provider SDK/HTTP clients, credentials and retries;
MCP/OTel bridges do not construct Python framework clients. Tokenmaster profiles
are supplied explicitly. Merge preparation uses memory rather than Python's
disk spool; a multi-source CLI merge is not globally atomic, and append-only
stores cannot retract acknowledged events. Runtime handles use single-thread
`Rc`; independent persistent-store runtimes coordinate through database
reservations.

Remote checks use local standalone services, including one MongoDB replica-set
member and one Neo4j server reached through routing discovery. They do not
establish multi-node failover, partition tolerance or remote throughput. Kafka
has no shared budget arbiter and requires a dedicated existing single partition
with unlimited retention; a process crash can lose its transient pending marker.
The TLS build check does not establish live certificate-authentication
interoperability. The NVML result verifies device-wide sampling, not an energy
saving attributable to this port.


## Completed performance measurements

All three measurement sets completed against source `be9e565` with unchanged
source and executable hashes. Receipts record `git_dirty: true` because evidence
and documentation were being assembled; their source maps and unchanged-source
guards identify the measured code. Raw samples, extrema, medians, import-path
checks and hashes are retained in [MemoryStore/identity](release-performance/performance.json),
[SQLite](release-performance/storage-performance.json) and
[stream memory/time](release-performance/stream-memory.json).

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

See the [tagged release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-rust-v0.2.1) for final CI, source, archive and registry receipts and publication status.
