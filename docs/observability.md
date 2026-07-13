# Observability

Pollard recordings are inspectable without a server, account, or network
connection. The core package installs a `pollard` command that reads SQLite
stores. Every command has a `--json` form for scripts and CI.

## List and inspect runs

```powershell
pollard runs runs.db
pollard show runs.db <root-id>
pollard report runs.db <root-id>
```

`show` prints an ASCII tree by default so its output is encoding-safe on
Windows and Linux terminals. `--unicode` opts into Unicode connectors.

```text
root 2db812ec support-triage
\-- model_call 616b8aa2 gpt-deployment charges[steps=1 tokens=214]
    |-- tool_call 29b07977 lookup_customer charges[steps=1]
    \-- refusal c2fd7941 budget:tokens [REFUSED]
```

The default output includes structure, short node ids, labels, charges,
refusals, and prune markers. It does not include payloads or results. Use
`--payloads` only when the destination is allowed to receive prompt and result
content:

```powershell
pollard show runs.db <root-id> --payloads
pollard show runs.db <root-id> --json --payloads
```

## Static HTML

```powershell
pollard show runs.db <root-id> --html run.html
```

The export is one self-contained HTML file with native collapsible sections,
inline CSS, no JavaScript, and no remote assets. Payloads and results are absent
unless `--payloads` is also present. Pruned nodes are dimmed and refusals are
highlighted.

## Integrity and seals

Verify one run or every root in a database:

```powershell
pollard verify runs.db <root-id>
pollard verify runs.db
pollard verify runs.db --json
```

The exit code is `0` for a clean recording, `1` when integrity findings exist,
and `2` for an invalid command or unreadable input. This makes verification
usable as a CI step.

Create a rolling subtree seal and optionally write the full report:

```powershell
pollard seal runs.db <root-id>
pollard seal runs.db <root-id> --output seal.json --json
```

## Charge reports

`pollard report` sums stored charges for a run or subtree. Hybrid hits also
accumulate avoided charges in mutable node metadata, so later CLI reports can
show historical avoided work. Pure replay stays read-only; its avoided charges
remain available from `run.report()` during that process. Mutable metadata is
excluded from the seal by design.

## OpenTelemetry

Install the bridge and configure any OpenTelemetry SDK and exporter your
application already uses:

```powershell
pip install "pollard[otel]" opentelemetry-sdk
```

Offline export preserves the Pollard tree as OpenTelemetry parent-child spans:

```python
from opentelemetry import trace
from pollard import SQLiteStore
from pollard.otel import export_spans

with SQLiteStore("runs.db") as store:
    count = export_spans(store, root_id, trace.get_tracer("my-agent"))
```

For spans as new nodes are recorded, pass the optional runtime callback:

```python
from opentelemetry import trace
from pollard import Runtime
from pollard.otel import live_span_hook

runtime = Runtime("runs.db", on_node=live_span_hook(trace.get_tracer("my-agent")))
```

The offline bridge should be preferred when exact span topology matters. A live
child may be recorded after its parent's live span ended, so the live bridge
uses `pollard.parent.id` for that relationship. A live callback failure emits a
runtime warning after the node is safely stored; telemetry failure does not
discard or interrupt the governed result.

The bridge emits current GenAI semantic-convention attributes where Pollard has
the required data, including `gen_ai.operation.name`, provider, request and
response model, and input and output token usage. Pollard-specific fields cover
node identity, kind, attempt, charges, avoided work, refusal reason, registry
digest, prune state, and result digest. See the OpenTelemetry
[GenAI attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).

Prompt, result, tool arguments, and tool outputs are never placed on spans by
this bridge. The tree keeps those values in the selected Pollard store; the
telemetry export carries structure and accounting only.
