# Changelog

All notable changes to pollard will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

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
