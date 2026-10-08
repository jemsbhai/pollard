# Rust pollardai 0.2.0

An earlier local validation matrix passed for the Rust 0.2.0 release candidate.
Hosted PR CI then reproduced a SQLite concurrency failure across all platform
jobs at source `8176e3a`: eight workers could exhaust the runtime's three
optimistic verification/accounting attempts while other workers committed
legitimate changes. A coherent-snapshot fallback is now implemented and targeted
checks pass. The new full matrix, hosted CI and final performance results remain
pending; the earlier 206/226 test counts below are historical checkpoint results.
This report records implementation and validation; it does not assert that the
crate has been uploaded or verified through a registry installation. Publication
scope is the Rust crate. The repository's separate npm package is already at
0.2.0; the Python release oracle remains `pollard==1.6.0`.

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

The SQLite contention fix keeps the existing three optimistic attempts and
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
targeted results, not a replacement full-matrix result. The cache tests cover
later external commits becoming visible,
tampering and missing ancestors, and fallback without historical cache promotion.

The earlier completed checks, before the snapshot fallback, are recorded below.
Optional tests include the default tests, and repeated toolchains are separate
executions of the same cases; these counts must not be added as unique coverage.

| Earlier checkpoint check | Completed result | Receipt |
| --- | --- | --- |
| Default tests | 206 passed on stable, 206 on Rust 1.74, and 206 in the optimized build | [Native matrix](release-validation/validation.json) |
| Optional features | 226 passed on stable and 226 on Rust 1.74; 33 explicitly ignored in each ordinary run | [Native matrix](release-validation/validation.json) |
| Live storage | 32 live tests passed, plus one Redis configuration check | [Remote run](release-remote-validation/summary.json) |
| NVIDIA hardware | One explicit device-wide NVML sampling test passed | [Hardware log](release-validation/nvml-hardware.log) |
| SQLite interchange | Recording/seals/custody, direct arbitration and mixed runtime budget/window checks passed | [Storage](release-validation/interop-sqlite.log), [arbitration](release-validation/interop-arbitration.log), [runtime](release-validation/interop-runtime.log) |
| Remote interchange | All five backends and their CLI selectors passed; Redis includes 11 bidirectional Decimal-text cases | [Results](release-remote-validation/results), [Redis](release-remote-validation/results/redis-interop.json), [CLI](release-remote-validation/results/cli-remote-inspection.json) |
| Formatting, lint and packaging | Stable/MSRV formatting, warnings-denied Clippy and crate package verification passed | [Native matrix](release-validation/validation.json), [package log](release-validation/package.log) |
| Fresh downstream installation model | Default Rust 1.74.0 Windows consumer and all-feature Rust 1.74.1 Linux consumer resolved, compiled and recorded/replayed successfully | [Default consumer](release-consumer-validation/default-rust174-windows/summary.json), [all-feature consumer](release-consumer-validation/all-rust174-linux/summary.json) |

The 33 optional-test ignores are the 32 live service tests and the GPU test,
executed separately above. The Windows optional matrix excludes vendored Kafka
TLS; the Linux all-feature consumer includes it. Fresh consumers generated their
own lockfiles instead of copying the library lockfile. These checks exposed and
fixed Rust 1.74 resolution failures that locked repository builds had hidden.
The [manifest](../../crates/pollardai/Cargo.toml) constrains incompatible
transitive versions, and the [CI workflow](../../.github/workflows/native.yml)
now repeats clean default/all-feature consumer resolution.

The earlier full native matrix and its corresponding remote run used
[`15e771aaca2d3e4b4b3fbe8aa833e121a5c5db3b`](https://github.com/jemsbhai/pollard/commit/15e771aaca2d3e4b4b3fbe8aa833e121a5c5db3b).
Both receipts include source hashes and confirm unchanged source during their
runs. The remote receipt additionally records pinned service images, commands,
Python driver versions and frozen executable hashes. All five uniquely labelled
test containers were removed successfully. The interrupted
[earlier remote checkpoint](release-remote-checkpoint/summary.json) deliberately
retains its failed source-change guard and is superseded by that completed run.

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
commit. Fresh-consumer receipts are also identified as checkpoints with their
own source hashes. Hosted CI at this later commit exposed the concurrent SQLite
failure described above. The snapshot correction is a subsequent source change
and needs its own completed full matrix and hosted CI result.

MongoDB needs an explicit distinction between the release oracle and the source
fix. The unchanged Python 1.6.0 wheel must use **`tz_aware=True`** for mixed
Python/Rust leases and windows on non-UTC hosts. Its default BSON UTC datetime
is naive, and calling `.timestamp()` interprets it as local time; the
[frozen-wheel check](release-remote-validation/results/mongodb-interop.json)
records the resulting approximately six-hour error on this host. Rust already
uses the server UTC epoch. The [Python source fix](../../src/pollard/stores/mongodb.py)
normalizes naive BSON datetimes to UTC and preserves explicit offsets, without
emulating the erroneous clock. That Python fix has **not been uploaded to
PyPI**. A separately identified [corrected-source live check](release-remote-validation/results/mongodb-corrected-source-interop.json)
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

Performance evidence is being collected under
[`release-performance/`](release-performance), with source fingerprints and the
pinned release oracle. Measurements at `8176e3a` precede the new snapshot fix;
the memory, storage and combined timing results are not yet a complete reviewed
measurement set for the corrected release source. This report makes no final
speedup or memory-reduction claim. Earlier timings remain checkpoint evidence.
The completed full matrix, hosted CI and final performance interpretation will
be added when available; no registry upload or post-publication consumer result
is claimed here.
