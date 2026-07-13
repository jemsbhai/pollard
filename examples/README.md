# Examples

All examples run from the repository root. Core examples are offline and need
no model-provider credential.

## Offline walkthroughs

| Script | Purpose |
|---|---|
| `01_governed_call.py` | Record one governed model-shaped call. |
| `02_best_of_n.py` | Branch from a shared prefix and choose a result. |
| `03_budget_stop.py` | Refuse a call before an exact budget is exceeded. |
| `04_firewall.py` | Gate a registered tool and refuse an unknown tool. |
| `05_replay_ci.py` | Record, hybrid-reuse, and replay a semantic step. |
| `06_phase4_benchmarks.py` | Reproduce the Phase 4 deterministic mock checks. |
| `07_phase7_storage.py` | Reproduce the Phase 7 storage acceptance checkpoint. |
| `08_phase8_scaleout.py` | Exercise merge and optional PostgreSQL scale-out. |
| `mcp_demo_server.py` | Serve a credential-free local MCP tool for the recipe. |

Run an offline example with its path, for example:

```powershell
python examples\03_budget_stop.py
```

## Formal evidence runners

`exp_001_local_model.py`, `exp_004_storage.py`, `exp_005_contention.py`, and
the three `exp_006_*` recording scripts are controlled evidence runners, not
quickstart scripts. They pin or record the environment, write raw artifacts,
and fail when a registered condition does not hold. None makes a hosted-model
request.

EXP-006 uses `exp_006_research.py`, `exp_006_code_fix.py`, and
`exp_006_mcp_household.py` to record its three case studies. Recheck the
committed artifacts without the model, MCP servers, optional dependencies, or
network access:

```powershell
$env:PYTHONPATH = (Resolve-Path src)
python examples\exp_006_verify.py
```

Use each script's `--help` output and the
[evidence index](https://github.com/jemsbhai/pollard/blob/main/evidence/README.md)
for the exact protocol, prerequisites, command form, and scope limitations.

## Live providers and frameworks

OpenAI, Anthropic, Azure OpenAI, Amazon Bedrock, LiteLLM cloud routes,
LangGraph, pydantic-ai, and MCP integration recipes live in the
[recipe collection](https://github.com/jemsbhai/pollard/tree/main/docs/recipes).
Provider recipes are opt-in and may cost money; they do not run in the test
suite.
