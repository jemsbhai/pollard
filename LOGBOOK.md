# Logbook

This file is append-only. New experiment plans and results are added at the
bottom. Results must not be edited to improve a claim after the run.

## 2026-07-13 EXP-001 Plan

Question: for a best-of-n tree with one shared prefix call and n suffix calls,
does measured Pollard token spend match the analytic expression `p + n*s`?

Hypothesis: with deterministic mock calls and zero output tokens, measured token
spend equals `p + n*s` for each row, while a naive rerun baseline costs
`n*(p+s)`.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Seeds: `0, 1, 2, 3, 4`.
- Branch counts: `2, 4, 8`.
- Prefix tokens: `1000 + seed*17`.
- Suffix tokens: `100 + seed*3`.
- Metrics: naive input tokens, Pollard input tokens, savings percent,
  prediction error percent.
- Local model, wall-clock, and joules: not run in this pass. No public claim
  will use those metrics.

Pass rule: every row has prediction error at or below 2 percent. Summary reports
mean and 95 percent CI across the five seeds for each branch count.

## 2026-07-13 EXP-002 Plan

Question: does a runaway loop stop before the next call after token budget
exhaustion is detected?

Hypothesis: with a token budget of 5 and deterministic calls that each settle 4
tokens, two calls execute, the third call is refused before execution, and
overshoot is at most one settle charge.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Runtime: in-memory store.
- Budget: `Budget(tokens=5)`.
- Call charge: 4 tokens.

Pass rule: refusal node kind is `refusal`, refusal meter is `tokens`, and
overshoot is less than or equal to 4 tokens.

## 2026-07-13 EXP-003 Plan

Question: does the registry firewall block an unregistered side-effect request?

Hypothesis: a request for `delete_everything` against a registry that only
contains `approved@1` records a refusal, does not execute the approved handler,
and records the registry digest in the refusal payload.

Conditions:

- Script: `examples/06_phase4_benchmarks.py`.
- Runtime: in-memory store with a one-action registry.
- Hostile request: `delete_everything` with a local path argument.

Pass rule: `executed` is false, refusal kind is `refusal`, refusal reason is
`policy`, and the refusal payload includes `registry_digest`.

## 2026-07-13 EXP-001, EXP-002, EXP-003 Result

Command:

```powershell
python examples\06_phase4_benchmarks.py
```

Environment:

- Platform: Windows-11-10.0.26200-SP0.
- Python: 3.12.2.
- Pollard: 0.4.0.

Outcome:

- EXP-001 passed for the deterministic mock pass. Local model, wall-clock, and
  joule metrics were not run.
- EXP-002 passed.
- EXP-003 passed.

Summary:

| Experiment | Key result |
| --- | --- |
| EXP-001 n=2 | mean savings 45.352634 percent, 95 percent CI half-width 0.098301, max prediction error 0.0 percent |
| EXP-001 n=4 | mean savings 68.028951 percent, 95 percent CI half-width 0.147451, max prediction error 0.0 percent |
| EXP-001 n=8 | mean savings 79.367109 percent, 95 percent CI half-width 0.172027, max prediction error 0.0 percent |
| EXP-002 | 2 calls executed, spent 8 tokens against limit 5, overshoot 3 tokens, bound 4 tokens |
| EXP-003 | hostile tool request executed false, refusal reason policy, registry digest recorded true |

Adversary review:

- The run only supports mock-token accounting claims.
- No README performance number was added from this pass.
- No local model, wall-clock, dollar, or joule claim should cite this entry.

## 2026-07-13 REC-005 Plan

Purpose: verify each Phase 5 live recipe once against the external stack it
documents.

Protocol:

- Run the five scripts under `docs/recipes/` with user-owned provider clients
  or an MCP session.
- Record package versions, model id, exit status, root id, and redacted console
  output.
- Do not treat syntax compilation or frozen adapter fixtures as a live provider
  verification.

Status: pending user credentials and a selected MCP server. No live result is
claimed by this entry.
