# Changelog

All notable changes to pollard will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

## [1.4.0] - 2026-07-30

### Added

- Add end-to-end LangChain incident-response and support-policy RAG, Pydantic
  refund, and pydantic-ai claim-triage recipes with deterministic offline
  execution, SQLite recording, and strict replay. Both LangChain recipes and
  the pydantic-ai recipe also provide an explicit OpenAI-backed `--live` mode.
- Accept non-empty JSON Schema `anyOf` arrays whose branches use Pollard's
  existing zero-dependency schema subset, including Pydantic nullable unions.
- Accept integer `minimum`, `maximum`, `exclusiveMinimum`, and
  `exclusiveMaximum` bounds, nonnegative string `minLength` and `maxLength`
  constraints, and nonnegative array `minItems` and `maxItems` constraints.
- Allow `pollard show`, `report`, `verify`, `seal`, and `export` to accept
  PostgreSQL store specifications and credential-safe `pg-env:` references
  while keeping `import` and `gc` SQLite-only.
- Add credential-safe `redis-env:VARIABLE?prefix=PREFIX#store-id` sources for
  `show`, `report`, `verify`, `seal`, `export`, `runs`, and merge input, plus
  Redis merge destinations. Sources and ordinary destinations require an
  existing logical namespace. Redis destinations require an explicit prefix
  and store id; `--initialize-if-missing` is the only opt-in to atomically
  initialize a missing `redis-env:` destination after every source has been
  fully traversed, validated, and finalized into private disk-backed preparation.
  Direct `redis://` and `rediss://`
  arguments remain legacy source-only forms and are rejected as destinations.
  Malformed percent escapes, ambiguous whitespace selectors, and Redis URL
  overrides of Pollard's text-decoding settings fail before client creation.
  Redis merge is idempotent on an exact rerun but is not a cross-node or
  cross-backend transaction. `import` and `gc` remain SQLite-only.
- Add environment-backed
  `mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id` sources for
  `show`, `report`, `verify`, `seal`, `export`, `runs`, and merge input, plus
  MongoDB merge destinations. Destinations are accepted only through
  `mongo-env:`, require an explicit database, collection prefix, and store id,
  and are existing-only by default. `--initialize-if-missing` explicitly
  initializes a fresh logical namespace after every source has been fully
  traversed. Direct MongoDB URI arguments remain rejected. Existing-only
  access performs no writes or index creation; missing, partial, ambiguous, or
  incompatibly indexed namespaces fail closed without implicit repair. Merge
  application is node-by-node and non-atomic, while an exact rerun is
  idempotent. `import` and `gc` remain SQLite-only.
- Add basic-auth
  `neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`
  sources for `show`, `report`, `verify`, `seal`, `export`, `runs`, and merge
  input, plus Neo4j merge destinations. Destinations are accepted only through
  `neo4j-env:`, require explicit URI, user, and password environment references,
  database, and store id, and are existing-only by default. Direct Neo4j and
  Bolt URI arguments remain rejected. `--initialize-if-missing` opts into a
  fresh logical namespace only after every source has been spooled and validated.
  Existing-only access validates the exact schema, coordinator, two shared
  global constraints, and their owned online range indexes without writing or
  issuing constraint DDL. Fresh setup
  creates both database-wide constraints in one schema transaction, or
  validates their exact existing state, outside the logical transaction, then
  initializes schema and coordinator together in one
  transaction. Partial or ambiguous logical state and partial, incompatible, or
  offline constraint/index state fail closed without implicit repair. Merge
  application is node-by-node and
  non-atomic, while an exact rerun is idempotent. `import` and `gc` remain
  SQLite-only.
- Add environment-backed
  `kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id` sources for
  `show`, `report`, `verify`, `seal`, `export`, `runs`, and merge input, plus
  narrow Kafka merge destinations. Destinations require an explicit
  configuration reference, topic, and store id, and accept only an existing
  populated topic whose fully replayed history proves that identity. Missing,
  empty, wrong-identity, corrupt, truncated, or incompatibly configured topics
  fail before producer construction and publish nothing. Direct `kafka://`
  broker specifications remain refused. Pollard never creates topics, and
  `--initialize-if-missing` remains invalid for Kafka. Read-only construction
  creates no producer and freezes one fully replayed topic prefix at its
  construction-time high watermark; writable CLI construction creates the
  producer only after topic, configuration, history, and prefix validation.
  Source preparation and spool validation finish before destination
  configuration lookup or access. Destination application is append-by-append
  and irreversible rather
  than atomic, while an exact rerun publishes no new events. Kafka remains
  neither an import nor a garbage-collection target and still provides no
  shared budget arbitration or record-level GC.

### Changed

- Bound aggregate CLI merge preparation memory by serializing each fully
  traversed and validated source, in deterministic order, into a unique private
  SQLite spool. Every spool is finalized and independently validated before the
  CLI reads destination environment values, constructs a destination client,
  initializes a namespace, or writes. Spools preserve exact node fields and
  per-source ordering, and cleanup is attempted on every exit. A cleanup failure
  can leave private artifacts and makes an otherwise successful command fail;
  combined failures preserve the primary attribution and add a fixed cleanup
  notice. Creation, serialization, disk-write, finalization, corruption, and
  truncation fail closed before destination access. Destination application
  remains node-by-node and non-atomic; exact reruns remain idempotent.

### Fixed

- Make Kafka reconnect and post-acknowledgement replay repair failure-atomic,
  close rejected replacement clients, and reject a changed prior record prefix.
- Keep a malformed Kafka command from poisoning an exact-offset replay retry.
- Fully traverse and materialize every CLI merge source before opening the
  destination so an invalid or unreadable source cannot leave an explicitly
  initialized Redis, MongoDB, or Neo4j destination behind, construct a Kafka
  producer, or publish a Kafka event.
- Make MongoDB reconnect and ordinary writes validate an existing exact
  coordinator and unique record index instead of recreating missing state.
  Fresh schema and coordinator revision state initialize in one MongoDB
  transaction; collection/index setup remains a separate physical operation.
- Make Neo4j reconnect and ordinary writes validate an existing exact
  coordinator and both required uniqueness constraints instead of repairing
  missing state. Reconnect performs read-only validation even when the store
  was originally opened with `create=True`.

## [1.3.0] - 2026-07-27

### Added

- Allow `TokenmasterMeter` to use a caller-supplied prompt estimator and output
  reservation for conservative token-budget prechecks before model dispatch.
- Add sync and async live model revalidation with caller-declared execution
  fingerprints, budgeted observation nodes, normalized or exact comparators,
  value-free drift evidence, and an offline walkthrough.

### Changed

- Document that strict replay returns the recorded result deterministically;
  checking a provider for behavioral drift is an explicit record-mode
  revalidation operation that preserves the golden recording.
- Validate `TokenmasterMeter` output reservations as nonnegative integers,
  rejecting ambiguous booleans, floating-point values, and negative values.

## [1.2.0] - 2026-07-23

### Added

- Add offline walkthroughs for retained streaming replay, schema-driven
  sensitive-field redaction, dry-run plus confirmation policy, and async model
  and tool execution.
- Add query-only `SQLiteStore(..., read_only=True)` access and use it
  automatically when a replay runtime is constructed from a database path.

### Changed

- Make strict replay require an existing, integrity-valid run root, note, and
  branch anchor; exact structural hits bypass current budget prechecks just like
  recorded model and tool results.
- Resolve registered-tool identity during strict replay without executing the
  current handler or policy hooks.
- Return detached node snapshots from in-process stores so callers cannot
  mutate stored payloads, results, or metadata through a prior read.

### Fixed

- Emit `Muntaser Syed` in the standalone core-metadata `Author` field so PyPI
  and pepy.tech can display the package author.
- Keep strict replay read-only on missing roots, notes, branches, registry
  bindings, and prune attempts.
- Report a missing stored ancestor as an integrity failure instead of leaking a
  backend `KeyError`.

### Security

- Fail closed when structural replay history or registry binding is absent, and
  prevent replay callers from changing future in-memory or HashRope results by
  mutating returned objects.

## [1.1.1] - 2026-07-22

### Added

- Add a caller-owned Redis client factory for Sentinel and managed-primary
  discovery without moving credentials or failover policy into Pollard.
- Add dedicated Redis, MongoDB, Neo4j, and Kafka production guides covering
  topology, security, timeouts, durability, monitoring, rotation, recovery,
  and acceptance testing.
- Add forced-writer-loss acceptance on a local three-primary Neo4j Enterprise
  evaluation cluster, including reconnect, retry-tombstone, strict-replay,
  verification, and external-seal custody checks.

### Fixed

- Keep the prior validated MongoDB client installed until a replacement has
  passed topology, index, and Pollard schema validation.
- Refuse a Redis reconnect factory that returns the currently installed client,
  which would otherwise be closed after replacement.

### Security

- Document least-privilege command and role boundaries, Redis no-eviction and
  Sentinel requirements, MongoDB discovery and timeouts, Neo4j routed cluster
  URIs, and Kafka minimum-ISR and unclean-election policy.

## [1.1.0] - 2026-07-22

### Added

- Add `RedisStore`, `MongoStore`, and `Neo4jStore` as transactional shared
  stores with exact Decimal reservations, lease renewal, permanent retry
  tombstones, server-time admission, and explicit reconnect uncertainty.
- Add `KafkaStore` as a single-partition, infinite-retention, append-only Store
  for deterministic audit and replay. It intentionally does not advertise
  shared arbitration or physical garbage collection.
- Add `redis`, `mongodb`, `kafka`, `neo4j`, and combined `stores` installation
  extras, plus real-service acceptance tests and operations documentation.
- Add a configured five-backend walkthrough that records, strictly replays,
  verifies, and seals a deterministic run without a model-provider request.

### Changed

- Raise the supported Python test surface through Python 3.14 and enforce an
  all-backend coverage gate above 90 percent.
- Update GitHub Actions checkout and Python setup actions to their current
  Node 24 based major versions.
- Expand remote-store selection, lifecycle, uncertainty, authorization,
  migration, Kafka provisioning, and recovery guidance before release.

### Fixed

- Avoid spurious Neo4j missing-property notifications while initializing an
  empty database, without weakening stored-record validation.
- Correct the configured walkthrough's repeat-run guidance: its settled budget
  is intentionally persistent, so reused run labels fail closed with an
  actionable instruction instead of being described as idempotent.

### Security

- Fail closed on unsupported remote-store schemas, incompatible Kafka topic
  retention or partitioning, MongoDB deployments without transactions, corrupt
  backend records, changed reservation retries, and changed settlement charges.

## [1.0.7] - 2026-07-21

### Added

- Retain provider-native token and cache breakdowns under `provider_usage`
  beside Pollard's normalized `usage` in the direct OpenAI, Anthropic, and
  Bedrock adapters.
- Record an `accounting_fallbacks` audit marker when missing or invalid provider
  usage is conservatively replaced by a precheck estimate.

### Fixed

- Settle dispatched reservations conservatively on operator interruption,
  `SystemExit`, asynchronous cancellation, and stream-consumer failure instead
  of releasing them as ordinary pre-dispatch errors.
- Surface OpenAI failed terminal responses and Anthropic error events with
  structured raw details, preserve OpenAI incomplete-response usage, and reject
  OpenAI, Anthropic, or Bedrock streams that end without the provider's required
  terminal event.
- Include Anthropic cache creation/read tokens and Bedrock cache write/read
  tokens in normalized input totals.
- Keep a nonzero token precheck estimate when a completed provider result omits,
  truncates, or corrupts normalized usage, while continuing to prefer valid
  settled provider usage.
- Record lease-renewal failures derived from `BaseException` instead of allowing
  the heartbeat worker to terminate silently.

## [1.0.6] - 2026-07-21

### Added

- Accept finite local JSON Schema references into `$defs` and `definitions`,
  with deterministic expansion and explicit refusal of missing, external, or
  cyclic references.
- Add a provider-neutral post-dispatch outcome signal and content-free failure
  notes for calls whose external result or local recording state is uncertain.

### Fixed

- Preserve the original identity payload when a caller mutates its request
  object during execution.
- Preserve native provider exceptions while direct adapters mark generation
  failures for conservative estimate settlement by the runtime.
- Keep meter and lease cleanup failures from masking the primary call error,
  bound lease shutdown, and conservatively account for completed calls that
  fail during result processing.
- Validate closed schemas that omit an explicit object type, distinguish JSON
  booleans from integers in enums, and reject empty or duplicate enum values.
- Project Anthropic token-count arguments independently from generation-only
  arguments, preserve structured Bedrock stream error events, normalize nested
  MCP SDK models, and export deep OpenTelemetry trees iteratively.
- Take reservation timestamps after the relevant write locks for SQLite
  reserve and settle, and PostgreSQL reserve, settle, release, and renewal.

## [1.0.5] - 2026-07-20

### Fixed

- Schedule reservation-renewal attempts at a fixed cadence so a slow database
  round trip does not postpone the next heartbeat, and report lease loss when
  call completion has passed the last conservatively confirmed deadline.

## [1.0.4] - 2026-07-20

### Fixed

- Accept the `title`, `description`, and `default` JSON Schema annotations
  generated by supported MCP Python SDK releases while continuing to reject
  unsupported validation keywords.

## [1.0.3] - 2026-07-20

### Added

- Add explicit PostgreSQL schema version 2, an administrator-run forward
  migration from the legacy unversioned schema, unknown-version refusal, and
  tested backup, restore, restart, and reconnect procedures.
- Add `SQLiteSealSink` as a reference external custody log for seal sequence,
  store ID, root ID, algorithm, digest, time, and signer identity.

### Fixed

- Renew SQLite and PostgreSQL reservations while model or tool calls run past
  their initial lease, preserving exact request and step admission under a
  healthy shared database.
- Make PostgreSQL reserve and settle retries idempotent, recover one lost
  connection acknowledgement, reject changed settlement charges, and report
  persistent ambiguity with typed exceptions.

## [1.0.2] - 2026-07-19

### Fixed

- Replace recursive depth-first tree traversal with deterministic iterative
  traversal in the memory, HashRope, SQLite, PostgreSQL, and subtree-manifest
  stores, preventing `RecursionError` on valid deep execution trees while
  preserving the existing preorder.

## [1.0.1] - 2026-07-13

### Changed

- Remove GitHub's package-publishing workflow and document the local-only,
  direct-to-production-PyPI release procedure and verification checkpoints.
- Expand the documentation and example indexes with complete prerequisites,
  credentials, cost boundaries, commands, outputs, failure modes, and evidence
  limitations.
- Expand direct OpenAI, Anthropic, Azure OpenAI, Amazon Bedrock, Vertex AI,
  Microsoft Foundry Models, LiteLLM, gateway, LangGraph, pydantic-ai, and MCP
  integration guidance against current primary documentation.
- Update the Anthropic tool-loop default to the pinned `claude-sonnet-5` model,
  force exactly one demo tool call, add strict schemas and credential preflight,
  and retain retry-free 128-token response caps.
- Add bounded LangGraph and pydantic-ai extras, require the Anthropic SDK that
  supports the documented effort request, and keep the MCP recipe on the
  compatible 1.x SDK line ahead of MCP Python SDK 2.0.
- Add an Azure OpenAI extra and a `DefaultAzureCredential` recipe path so the
  same Azure example covers API-key and Microsoft Entra ID authentication.
- Add one public API reference for runtime construction, run cursors, step
  results, budgets, meters, registries, policies, replay, stores, integrity,
  async calls, adapters, and exceptions.

## [1.0.0] - 2026-07-13

### Added

- Add EXP-006 end-to-end research, code-fix, and local MCP household case
  studies with pinned inputs, verify-clean SQLite recordings, subtree seals,
  and content-free HTML trees.
- Add a combined evidence manifest and a zero-dependency offline verifier that
  checks hashes, nodes, seals, registry digests, local-path and credential
  leakage, and all six strict-replay paths without executing model or tool
  functions.
- Add stranger-verification, recording, interpretation-boundary, and 1.0
  launch documentation for the case-study artifacts.

### Changed

- Activate the 1.0 compatibility covenant for node identity, canonical
  identity serialization, the public `Store` protocol, and synchronous and
  asynchronous step-function contracts.
- Mark the package as production/stable and complete the Phase 9 evidence and
  reviewer-adversary checkpoints.

## [0.9.0] - 2026-07-13

### Added

- Add formal EXP-001 local-model, EXP-004 storage-curve, and EXP-005
  multi-version PostgreSQL contention runners with committed raw artifacts.
- Add a machine-checked evidence index, adversarial claim boundaries, and
  reproduction guidance with no hosted-provider dependency.
- Publish `Store` at the package root and document the proposed 1.0 identity,
  canonical serialization, store, and step-function stability covenant.
- Add a 90-day and one-minor-release deprecation policy for non-frozen public
  APIs after 1.0.

### Changed

- Prefer NVML's cumulative energy counter when available, with sampled power as
  the compatibility fallback.
- Serialize PostgreSQL first-use schema creation and close a window-settlement
  locking gap found by EXP-005.
- Update OpenAI examples to GPT-5.6 defaults, disable Responses storage in the
  direct examples, and document current Azure, Bedrock, LiteLLM, and credential
  boundaries.
- Enforce absolute Markdown links across every repository README so the same
  links work on PyPI.

## [0.8.0] - 2026-07-13

### Added

- Add idempotent, conflict-aware store merge with conservative metadata union
  and replay-mode result conflict rejection.
- Add `PostgresStore` through `pollard[pg]`, including payload interning,
  logical-store isolation, benign concurrent puts, and row-locked metadata.
- Add store-backed `WindowMeter` request and token ceilings with refusal window
  context shared across writers and resumes.
- Add transactional budget reserve and settle state with expiring leases for
  SQLite and PostgreSQL.
- Add multi-store `pollard runs` and `pollard merge`, a PostgreSQL CI service
  job, and repeated two-process contention acceptance coverage.

## [0.7.0] - 2026-07-13

### Added

- Add transparent SQLite payload interning with configurable thresholds and
  identity parity when interning is disabled.
- Add redact-before-hash markers and automatic redaction for registry schema
  string fields marked `sensitive: true`.
- Add explicit `gc()` drop-pruned and compact modes with survivor seals.
- Add sealed subtree export and verified import APIs plus `gc`, `export`, and
  `import` CLI commands.
- Add compliance-oriented documentation describing stored fields, retention,
  redaction limits, and operator responsibilities.

## [0.6.0] - 2026-07-13

### Added

- Add a direct Amazon Bedrock Converse adapter with streaming, tool-use, usage,
  and opt-in CountTokens support against frozen fixtures.
- Document Azure OpenAI through the existing OpenAI adapter and cloud routes
  such as Vertex AI through the LiteLLM adapter.
- Add `pollard show`, `report`, `verify`, `seal`, and `runs` with JSON output,
  privacy-safe defaults, and a self-contained HTML tree export.
- Add an optional OpenTelemetry bridge for offline topology-preserving export
  and live node callbacks.

## [0.5.0] - 2026-07-13

### Added

- Add sync and async stream consumption with ordered delta callbacks, optional
  retained chunks, replay re-emission, and one settle at stream completion.
- Add input token estimators with explicit output reservations and estimated
  budget-refusal markers.
- Add OpenAI, Anthropic, and LiteLLM adapters behind optional extras, tested
  against frozen response fixtures.
- Add a tiktoken-backed OpenAI estimator and an Anthropic count-tokens estimator.
- Add live cookbook recipes for provider tool loops, LangGraph, pydantic-ai,
  and MCP registry gating.

## [0.4.0] - 2026-07-13

### Added

- Add an optional hashrope-backed store with append-only log snapshots.
- Add an optional tokenmaster-backed token meter with node metadata for state and advice.
- Add `seal()` for rolling export digests over node ids and result digests.
- Add Phase 4 offline benchmark script, logbook, and findings index.
- Add launch plan notes for the v0.4 evidence wave.

## [0.3.0] - 2026-07-13

### Added

- Add record, hybrid, and replay runtime modes with avoided-charge accounting.
- Add `MissingRecording` and replay integrity checks before serving stored results.
- Add the `pollard_run` pytest fixture and `--pollard-mode` option.
- Add a committed replay recording and CI test that runs with sockets guarded.

## [0.2.0] - 2026-07-13

### Added

- Add a versioned action registry with schema validation and registry digests.
- Add firewalled tool calls, policy denial, confirmation tokens, and dry-run mode.
- Add async runtime parity for model and tool calls.
- Add an MCP tools/list adapter for declared tool registries.

## [0.1.0] - 2026-07-13

### Added

- Add content-addressed execution tree nodes and canonical identity hashing.
- Add memory and SQLite stores with verification support.
- Add budgets, meters, refusal nodes, and sync runtime calls.
- Add offline examples for governed calls, branching, and budget stops.

## [0.0.1] - 2026-07-13

### Added

- Reserve the pollard package name.
- Add the initial package skeleton.
