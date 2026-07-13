# Changelog

All notable changes to pollard will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

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
