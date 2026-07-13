# Changelog

All notable changes to pollard will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

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
