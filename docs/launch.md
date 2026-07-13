# Launch Plan

Each release gets one announcement angle. Any performance or savings number must
cite an `EXP-XXX` entry from `LOGBOOK.md`.

## Wave 1: v0.1

Audience: Python agent builders and local inference users.

Angle: governed execution trees. Show budget refusal, branch and rollback, and
the audit tree.

Demo source: `examples/03_budget_stop.py`.

## Wave 2: v0.2

Audience: agent security and tool-governance users.

Angle: structural registry gating. Show an unregistered request dying at the
registry before any handler runs.

Demo source: `examples/04_firewall.py`.

## Wave 3: v0.3

Audience: teams testing model-agent workflows.

Angle: offline deterministic tests. Show replay mode serving a stored result
with no live client call.

Demo source: `examples/05_replay_ci.py`.

## Wave 4: v0.4

Audience: users who need ecosystem hooks and export evidence.

Angle: optional stores, tokenmaster telemetry, export seals, and logged evidence.

Demo sources:

- `HashRopeStore` snippet in `README.md`.
- `TokenmasterMeter` snippet in `README.md`.
- `seal()` snippet in `README.md`.
- `examples/06_phase4_benchmarks.py` for mock-only evidence.

Allowed claims:

- Hashrope and tokenmaster are optional extras.
- `seal()` produces a rolling SHA-256 report over node ids and result digests.
- Mock-token savings and budget/firewall outcomes may cite the
  `2026-07-13 EXP-001, EXP-002, EXP-003 Result` logbook entry.

Disallowed claims until measured:

- Local model speed.
- GPU joules.
- Hosted-provider cost savings.
