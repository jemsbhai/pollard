# pollard

Governed execution trees for AI agents: budget it, gate it, replay it.

```powershell
pip install "pollard[openai]"
```

```python
from openai import OpenAI
from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn

with Runtime("runs.db").run("triage", budget=Budget(tokens=20_000)) as run:
    node = run.model_call({"model": "gpt-5.5", "input": "Summarize: ..."},
                          fn=make_responses_fn(OpenAI()))
    print(node.result["text"], run.report())
```

pollard is a runtime primitive, not an agent framework. It records each step as a node in a content-addressed tree. Node identity is a hash of the step inputs, parent identity, kind, and attempt number, so the tree gives you a control-flow ledger without owning your model client, tools, prompts, or loop.

The client above belongs to your code. Pollard does not read credentials or
construct provider clients. Anthropic, Amazon Bedrock, and LiteLLM adapters
follow the same pattern through `pollard[anthropic]`, `pollard[bedrock]`, and
`pollard[litellm]`. Azure OpenAI uses the OpenAI adapter with an Azure-configured
client. See [Cloud-hosted model providers](https://github.com/jemsbhai/pollard/blob/main/docs/cloud-providers.md)
for direct AWS and Azure examples plus Vertex AI and other LiteLLM routes.

What you get:

- Budget: refuse a step before it runs when a known budget would be exceeded.
- Branch and rollback: make alternate children, move the cursor back, and keep shared history.
- Audit: each node id commits to its ancestry and identity payload.
- Registry firewall: registered tool calls resolve against a versioned action set or fail closed.
- Replay: record semantic steps once, then serve stored results in tests and CI.

Budget semantics are honest about what can be controlled. If a precheck estimate proves a step would exceed budget, pollard records a refusal node and does not call your function. If the actual result charge exceeds budget after the function returns, that node still stands because the spend already happened; later steps are refused.

Current limits:

- Replay of sampled model calls serves the recorded output. It does not re-check that a provider would return that output again.
- Hosted API energy use is not measured. The NVML energy meter is for local GPU inference only.
- A SQLite store assumes one writer process.
- HashRopeStore is an in-process operation-log backend, not a multi-writer
  database. Explicit offline garbage collection rewrites its snapshot.
- TokenmasterMeter reports tokenmaster state from the usage data your model client returns; it does not tokenize prompts itself.
- Prompt estimators are approximations. Images, tool schemas, provider-added
  instructions, and wire-format changes can make the settled usage differ.
- The audit tree is tamper-evident, not tamper-proof. Verification detects changed history, but it cannot stop deletion of the whole store file.

## Offline Mock Demo

Core Pollard still installs with zero runtime dependencies and can be tried
without a provider account:

```python
from pollard import Budget, Runtime
from examples.mock_model import call_model

with Runtime().run("offline", budget=Budget(tokens=100)) as run:
    node = run.model_call({"model": "mock-1", "messages": []}, fn=call_model)
    print(node.result["text"])
```

## Streaming And Estimates

A model function may return a result dictionary or an iterator of chunk
dictionaries. `model_call(..., on_delta=callback)` forwards chunks in order.
With `keep_chunks=True`, Pollard stores those chunks under `result["chunks"]`
and re-emits them through the callback during replay. Charges settle once, after
the stream ends, and node identity remains a function of the input payload.

`TokenMeter(estimator=..., reserved_output_tokens=N)` applies an estimated input
charge plus an explicit output reservation at precheck. A refusal caused by that
estimate records `{"estimated": "true"}`. The settled provider usage remains the
source of actual token charges.

The optional tiktoken estimator is available as:

```python
from pollard.estimators.openai import OpenAITokenEstimator
from pollard.meters import TokenMeter

meter = TokenMeter(OpenAITokenEstimator(), reserved_output_tokens=1024)
```

See the [recipe collection](https://github.com/jemsbhai/pollard/tree/main/docs/recipes)
for full tool loops and integration patterns.

## Observability

The core package includes an offline CLI for SQLite recordings:

```powershell
pollard runs runs.db
pollard show runs.db <root-id>
pollard report runs.db <root-id> --json
pollard verify runs.db
pollard show runs.db <root-id> --html run.html
```

`show` defaults to an ASCII, content-free tree. Payloads and results require an
explicit `--payloads` flag. The HTML export is one static file with no remote
assets. The optional `pollard[otel]` bridge exports the same node topology to a
caller-configured OpenTelemetry tracer without placing prompt or result content
on spans. See [Observability](https://github.com/jemsbhai/pollard/blob/main/docs/observability.md)
for CLI exit codes, JSON forms, seals, HTML, and OpenTelemetry examples.

## Storage And Data Governance

`SQLiteStore` transparently interns repeated payload strings of at least 1 KiB.
Interning changes only the SQLite encoding. Callers receive the original payload,
and node ids are identical with interning on or off.

Redaction is separate. `redact(value, hint=None)` replaces a value before node
identity is computed, so the plaintext never reaches a Pollard store. Registry
schemas can apply the same rule automatically:

```python
from pollard import ActionSpec

def send_message(_args):
    return {"queued": True}

spec = ActionSpec(
    "send",
    "1",
    "Send a message.",
    {
        "type": "object",
        "properties": {"token": {"type": "string", "sensitive": True}},
        "required": ["token"],
    },
    True,
    handler=send_message,
)
```

The handler receives the original `token`; the audit payload stores only its
digest marker. Results and mutable metadata are not automatically redacted, so
handlers must not copy secrets into their return values.

```powershell
pollard gc runs.db drop-pruned
pollard gc runs.db compact
pollard export runs.db <root-id> subtree.json
pollard import subtree.json archive.db
```

See [Data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md)
for the field-level storage model, retention behavior, and redaction limits.

## Branch, Rollback, And Shared Prefixes

`run.branch()` creates an alternate child cursor while leaving the parent cursor
unchanged. `run.rollback()` moves a cursor to an ancestor, and `run.prune()`
marks an unwanted tip without deleting history. Identical calls beneath the same
parent compute the same node id, so hybrid and replay modes reuse recorded
prefixes before branches diverge.

EXP-001 measured this behavior only with deterministic mock token accounting.
Its local-model, wall-clock, dollar, and joule legs remain unrun. See the
[logbook](https://github.com/jemsbhai/pollard/blob/main/LOGBOOK.md) and
[findings](https://github.com/jemsbhai/pollard/blob/main/findings.md) for the
exact scope and results.

## Registry Firewall

With a registry installed, `tool_call` cannot execute an arbitrary caller-supplied function. The runtime resolves the tool name and version against `ActionSpec`, validates arguments against the supported schema subset, records the `spec_digest` and `registry_digest`, then runs the registered handler. Unknown tools, version mismatch, invalid args, policy denial, and missing confirmation all produce refusal nodes.

This is structural gating, not content judgment. A content firewall tries to decide whether a requested action is safe. pollard answers a narrower audit question: was this action in the declared, versioned set, with arguments that match its schema, under the recorded policy state?

Dry-run mode records side-effectful registered actions without executing their handlers. This is useful for reviewing an intended action transcript before allowing writes.

How it compares:

- LangGraph and related graph runtimes execute a graph you author ahead of time. pollard ledgers the control flow your code performs and can wrap calls inside a graph node.
- pydantic-ai, smolagents, and the OpenAI Agents SDK own more of the agent loop. pollard is bring-your-own-client and has zero core runtime dependencies.
- Action firewall products judge tool calls by content policy. pollard uses structural registry gating: an action resolves against a versioned registry or it does not execute.
- HTTP recorders pin transport bytes. pollard pins semantic steps, so recordings can outlive SDK or provider changes.

## Record And Replay

`Runtime(mode=...)` accepts three modes:

- `record`: execute the function and store the result.
- `hybrid`: serve a stored result when the computed node id already exists, otherwise execute and store.
- `replay`: never call the function. A missing result raises `MissingRecording`.

Replay mode verifies the stored node ancestry before serving a result. When `hybrid` or `replay` serves a stored result, `run.report()["avoided"]` records the charges that were skipped for that run.

For pytest, install pollard with the `dev` extra or with pytest available, then use the fixture:

```python
def test_agent(pollard_run):
    node = pollard_run.model_call(payload, fn=real_client)
    assert "invoice" in node.result["text"].lower()
```

Run with `--pollard-mode=record`, `--pollard-mode=hybrid`, or `--pollard-mode=replay`. The fixture stores small SQLite recordings under `tests/pollard_recordings/` by default.

## Export Seals

`seal(store, root_id)` returns a rolling SHA-256 report over a subtree's node ids
and result digests. The final digest can be stored beside an exported run:

```python
from pollard import Runtime, seal

rt = Runtime()
with rt.run("audit") as run:
    run.note({"status": "ready"})
    report = seal(run.store, run.root_id)

print(report.digest)
print(report.to_dict())
```

The seal validates each visited node before hashing it. Mutable metadata is not
included; see [Export seals](https://github.com/jemsbhai/pollard/blob/main/docs/seal.md)
for the field-level design.

## Store Backends

Core pollard includes `MemoryStore` and `SQLiteStore`. The optional hashrope backend keeps an append-only operation log inside a hashrope rope:

```powershell
pip install "pollard[hashrope]"
```

```python
from pollard import HashRopeStore, Runtime

store = HashRopeStore()
with Runtime(store).run("hashrope-demo") as run:
    run.note({"checkpoint": "stored in a hashrope log"})

snapshot = store.to_bytes()
reopened = HashRopeStore(snapshot)
assert reopened.get(run.root_id).payload == {"run": "hashrope-demo"}
```

See the [offline examples](https://github.com/jemsbhai/pollard/tree/main/examples)
for scripts that run without network access.

## Tokenmaster Meter

The optional tokenmaster meter records Pollard model-call usage into tokenmaster and stores the resulting gauge plus advice on each node:

```powershell
pip install "pollard[tokenmaster]"
```

```python
from pollard import Budget, Runtime
from pollard.meters import StepMeter, TokenmasterMeter

rt = Runtime(
    meters=[
        StepMeter(),
        TokenmasterMeter(model="anthropic:claude-sonnet-4-6", expected_remaining_turns=5),
    ]
)

with rt.run("tokenmaster-demo", budget=Budget(tokens=120_000, steps=20)) as run:
    node = run.model_call(
        {"model": "anthropic:claude-sonnet-4-6"},
        fn=lambda _payload: {"usage": {"input_tokens": 1000, "output_tokens": 300}},
    )
    print(node.meta["charges"]["tokens"])
    print(node.meta["tokenmaster"]["state"]["zone"])
```

Use `TokenmasterMeter` instead of the built-in `TokenMeter` when you want tokenmaster state and recommendations in the audit record. The budget charge remains the per-call token volume, including cache and reasoning token fields when present.

## Evidence

Phase 4 adds the [experiment logbook](https://github.com/jemsbhai/pollard/blob/main/LOGBOOK.md)
and [findings index](https://github.com/jemsbhai/pollard/blob/main/findings.md).
README performance numbers are intentionally absent until a logged run supports
the same scope.
