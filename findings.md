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
