# pollard

Governed execution trees for AI agents: budget it, gate it, replay it.

```powershell
pip install pollard
```

```python
from pollard import Budget, Runtime
from examples.mock_model import call_model

rt = Runtime("runs.db")
with rt.run("triage", budget=Budget(tokens=120_000, depth=8)) as run:
    node = run.model_call(
        {"model": "mock-1", "messages": [{"role": "user", "content": "Summarize: ..."}]},
        fn=call_model,
    )
    print(node.result["text"])
    print(run.report())
```

pollard is a runtime primitive, not an agent framework. It records each step as a node in a content-addressed tree. Node identity is a hash of the step inputs, parent identity, kind, and attempt number, so the tree gives you a control-flow ledger without owning your model client, tools, prompts, or loop.

What you get:

- Budget: refuse a step before it runs when a known budget would be exceeded.
- Branch and rollback: make alternate children, move the cursor back, and keep shared history.
- Audit: each node id commits to its ancestry and identity payload.
- Registry firewall: registered tool calls resolve against a versioned action set or fail closed.
- Replay-ready records: results live at nodes, separate from node identity, for later record and replay work.

Budget semantics are honest about what can be controlled. If a precheck estimate proves a step would exceed budget, pollard records a refusal node and does not call your function. If the actual result charge exceeds budget after the function returns, that node still stands because the spend already happened; later steps are refused.

Limits in v0.2:

- Replay of sampled model calls is not included until v0.3.
- Hosted API energy use is not measured. The NVML energy meter is for local GPU inference only.
- A SQLite store assumes one writer process.
- The audit tree is tamper-evident, not tamper-proof. Verification detects changed history, but it cannot stop deletion of the whole store file.

## Registry Firewall

With a registry installed, `tool_call` cannot execute an arbitrary caller-supplied function. The runtime resolves the tool name and version against `ActionSpec`, validates arguments against the supported schema subset, records the `spec_digest` and `registry_digest`, then runs the registered handler. Unknown tools, version mismatch, invalid args, policy denial, and missing confirmation all produce refusal nodes.

This is structural gating, not content judgment. A content firewall tries to decide whether a requested action is safe. pollard answers a narrower audit question: was this action in the declared, versioned set, with arguments that match its schema, under the recorded policy state?

Dry-run mode records side-effectful registered actions without executing their handlers. This is useful for reviewing an intended action transcript before allowing writes.

How it compares:

- LangGraph and related graph runtimes execute a graph you author ahead of time. pollard ledgers the control flow your code performs and can wrap calls inside a graph node.
- pydantic-ai, smolagents, and the OpenAI Agents SDK own more of the agent loop. pollard is bring-your-own-client and has zero core runtime dependencies.
- Action firewall products judge tool calls by content policy. pollard v0.2 will add structural registry gating: an action resolves against a versioned registry or it does not execute.
- HTTP recorders pin transport bytes. pollard pins semantic steps, so recordings can outlive SDK or provider changes.

See `examples/` for offline scripts that run without network access.
