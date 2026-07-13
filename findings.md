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
