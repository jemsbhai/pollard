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

## 2026-07-13 REC-005 Partial Result 1

Environment:

- Platform: Windows 11.
- Python: 3.12.2.
- Pollard: 0.5.0.
- OpenAI SDK: 2.38.0.
- MCP SDK: 1.26.0.

OpenAI tool-loop attempt:

- Script: `docs/recipes/openai_tool_loop.py`.
- Model: `gpt-5.5`.
- Exit status: failed before a provider response.
- Redacted error: HTTP 429, `insufficient_quota`; the configured API project
  requires usable billing or credits.
- Root id: none emitted by the recipe.

MCP registry result:

- Client script: `docs/recipes/mcp_registry.py`.
- Server: `examples/mcp_demo_server.py`, MCP stdio transport.
- Tool: `search` with the deterministic query `pollard`.
- Exit status: passed.
- Root id:
  `896e094b73da866da189ebfe83ce12ab8c75d6d3917605cf87574e6dd142ce7a`.
- Redacted output: one successful structured match for the local Pollard
  documentation record; no credentials or external data were involved.

Remaining live checks:

- `langgraph_node.py` and `pydantic_ai_wrap.py` use the same OpenAI account and
  are held until that account has usable quota. `pydantic-ai` also remains to
  be installed for its recipe.
- `anthropic_tool_loop.py` is held because `ANTHROPIC_API_KEY` is not configured.
- REC-005 remains incomplete; this partial result does not claim the provider
  recipes passed.

## 2026-07-13 v0.5.0 Local Release Checkpoint Result

The credential-free release gates were rerun after adding the local MCP live
path:

- Full suite: 156 tests passed.
- Coverage: 91.00 percent against a 90 percent floor.
- Ruff: passed.
- Mypy strict mode: passed for 29 source files.
- Writing-standards scan: passed.
- L0+L1 core size: 1,499 nonblank, noncomment lines against the 1,500-line
  limit.
- Build: `pollard-0.5.0.tar.gz` and
  `pollard-0.5.0-py3-none-any.whl` built successfully.
- Twine validation: both distributions passed.
- Clean wheel install: `pollard[mcp]` installed in a new virtual environment,
  imported as version 0.5.0, and completed the MCP governed-call recipe against
  MCP SDK 1.28.1.

External release status at this checkpoint:

- PyPI latest remains 0.4.0; version 0.5.0 has not been uploaded.
- TestPyPI was not attempted because TestPyPI authentication is not configured
  and REC-005 is still incomplete.
- No production upload, tag, push, or GitHub release was attempted.

## 2026-07-13 REC-005 Final Result

The user added provider credits, supplied the Anthropic credential through the
Windows user environment, directed the release to skip TestPyPI, and capped
available credit at 5 USD per provider. Before paid calls, every provider recipe
was changed to disable SDK retries and limit each response to 128 output tokens.

Environment:

- Python: 3.12.2.
- Pollard: 0.5.0.
- OpenAI SDK: 2.45.0.
- Anthropic SDK: 0.116.0.
- LangGraph: 1.2.9.
- pydantic-ai-slim: 2.9.0.
- MCP SDK: 1.28.1 for the clean-environment replay of the MCP recipe.

Results:

| Recipe | Model or server | Input tokens | Output tokens | Root id | Result |
| --- | --- | ---: | ---: | --- | --- |
| OpenAI tool loop | `gpt-5.5` | 159 | 28 | `44e86b6e8de9b6ed9f3075472c4ba5d5b039c850c85b4e8fdac1005c5737dafc` | passed |
| LangGraph node | `gpt-5.5` | 10 | 128 | `083ed1d4657c7c62ced94184d1b723800cf8bdbb2b409862c14d6605a22f8bba` | passed |
| pydantic-ai wrapper | `gpt-5.5` | 10 | 128 | `2005f48739a3e4afa5d7665537806dfab02791e05723d37ac4ee1484862c41e2` | passed |
| Anthropic tool loop | `claude-sonnet-4-6` | 1,244 | 97 | `2f9120727333664e2d35d8054ad7655d71159bcafab9403728b8b39ed588afd8` | passed |
| MCP registry | local stdio server | not applicable | not applicable | `896e094b73da866da189ebfe83ce12ab8c75d6d3917605cf87574e6dd142ce7a` | passed |

Cost calculation at the standard published rates checked on 2026-07-13:

- GPT-5.5 at 5 USD per million input tokens and 30 USD per million output
  tokens: 179 input and 284 output tokens across three recipes, approximately
  0.009415 USD.
- Claude Sonnet 4.6 at 3 USD per million input tokens and 15 USD per million
  output tokens: approximately 0.005187 USD.
- Combined estimated provider cost: approximately 0.014602 USD.

Live findings and corrections:

- The Anthropic precheck initially stopped locally because `max_tokens` was
  forwarded to the current SDK's `count_tokens` method. No billable message
  request occurred on that attempt. The adapter now strips create-only fields,
  with a regression test.
- The successful Anthropic calls were stored before Windows cp1252 output failed
  on a sun symbol. Provider recipes now configure UTF-8 output. The corrected
  display was verified from the stored nodes using an intentionally invalid API
  key, so the verification could not make another paid request.

REC-005 passed. All five live recipes produced governed roots, and measured
provider cost stayed far below the user's 5 USD limit on each account.
