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

## Wave 5: v0.5

Audience: practitioners bringing an existing model client or agent stack.

Angle: "pollard now speaks your stack." Show provider adapters, streaming,
precheck estimates, and runnable stack recipes. The cost figures in REC-005 may
be cited only as the measured cost of that release verification, not as a
general savings claim.

Demo sources:

- `docs/recipes/openai_tool_loop.py`.
- `docs/recipes/anthropic_tool_loop.py`.
- `docs/recipes/langgraph_node.py`.
- `docs/recipes/pydantic_ai_wrap.py`.
- `docs/recipes/mcp_registry.py`.

## Wave 6: v0.6

Audience: operators who need to inspect or export an agent audit trail.

Angle: "see every step your agent took." Show the content-free ASCII tree,
then the self-contained HTML export and the same topology in OpenTelemetry.

Demo commands:

```powershell
pollard show runs.db <root-id>
pollard show runs.db <root-id> --html run.html
pollard verify runs.db --json
```

Allowed claims:

- The default CLI and HTML output does not include payload or result content.
- HTML export is self-contained and has no JavaScript or remote assets.
- Offline OpenTelemetry export preserves Pollard parent-child topology.
- Direct Bedrock, Azure OpenAI, Azure AI, Vertex AI, and LiteLLM paths are
  documented and tested with frozen or local fixtures unless a live run is
  explicitly identified in `LOGBOOK.md`.

## Wave 7: v0.7

Audience: teams retaining agent audit records under storage and data-handling
requirements.

Angle: "an agent audit trail your data-governance team can approve." Show a
sensitive registry field reaching its handler while the stored node contains
only a digest marker, then export, import, and explicitly reclaim a pruned
branch.

Demo commands:

```powershell
pollard show runs.db <root-id> --payloads
pollard export runs.db <root-id> subtree.json
pollard import subtree.json archive.db
pollard gc runs.db drop-pruned
pollard gc runs.db compact
```

Allowed claims:

- SQLite interning preserves canonical payload bytes and node identity.
- Sensitive registry string fields are redacted before hashing and storage.
- Sealed imports are verified completely before nodes are written.
- Garbage collection is explicit and returns seals for surviving roots.

Storage-size figures remain logbook checkpoint data until EXP-004 formalizes
the protocol in Phase 9.
