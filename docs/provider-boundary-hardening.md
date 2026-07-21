# Provider boundary hardening

This note records the package-engineering review prompted by a completed external
evaluation. The evaluation repository and its submitted evidence are separate
from this package and were not changed.

## Classification

| Observed failure | Classification | Pollard boundary |
|---|---|---|
| Provider strict-tool count and schema limits | Experiment runner defect plus external provider constraint | Keep provider limits in caller-owned request construction. Pollard validates its registry and offers local reference expansion without selecting tools. |
| Local `$defs` and `$ref` rejected by a provider SDK | Experiment runner serialization defect | Pollard can resolve supported local references before a caller lowers a schema to a provider dialect. |
| Token-count and generation requests serialized differently | Experiment runner serialization defect | Direct adapters use an explicit token-count field projection. Callers remain responsible for unsupported providers and SDK versions. |
| More than one tool call returned | External provider behavior plus caller policy | Pollard preserves every returned call. The caller must request sequential behavior where supported and validate cardinality before executing tools. |
| Model accepted by metadata or token count but unavailable for generation | External provider behavior | Metadata and token-count success are not generation-availability guarantees. Model replacement and routing stay outside Pollard. |
| Provider error details lost before final reporting | Experiment runner defect and Pollard hardening opportunity | Native exceptions pass through. Structured adapter errors retain machine-readable fields, while Pollard failure nodes remain content-free. |
| Child process continued after the controlling terminal stopped | Experiment runner supervision defect | Process ownership, termination, and reconciliation stay outside Pollard. Lease shutdown inside Pollard is bounded. |
| A dispatched request failed with unknown billing outcome | Pollard hardening opportunity | An explicit provider-neutral unknown-outcome signal settles conservative estimates and records a content-free failure node. |

## Unknown outcomes

Ordinary exceptions retain the existing behavior: Pollard releases an active
reservation and re-raises the original error. A callable that knows an external
operation was dispatched but cannot determine its outcome can mark that case:

```python
from pollard import PostDispatchOutcomeUnknown

def call_provider(payload):
    try:
        return client.generate(**payload)
    except Exception as error:
        raise PostDispatchOutcomeUnknown(error) from error
```

Pollard settles the precheck estimates for that step, records only the blocked
payload digest and exception type, then re-raises the wrapped error as the
top-level exception. Raw response bodies, prompts, credentials, and exception
messages are not copied into the failure node. Retry decisions remain with the
caller.

Direct OpenAI, Anthropic, Bedrock, and LiteLLM adapters apply the same signal
automatically around generation and stream processing while preserving the
native exception type. Token-count calls remain precheck operations. If a
callable returns but local meter or result processing fails, Pollard records a
separate `call_recording_failed` note and settles the same conservative
estimates instead of releasing the reservation.

## Schema boundary

Pollard accepts its documented zero-dependency schema subset. Local references
may use JSON Pointer paths into `$defs` or legacy `definitions`. Expansion is
deterministic and refuses missing targets, external references, and cycles.
Object closure still follows JSON Schema semantics: omitted
`additionalProperties` is open, while `additionalProperties: false` is closed.

Reference expansion is not a claim that the resulting schema is accepted by
every model provider. Provider strict modes support different subsets and can
change independently. The caller must validate the final request against the
selected provider and SDK.

## Deterministic validation matrix

The release gate is frozen before any optional paid request:

- schema vectors for nested and escaped local references, missing targets,
  cycles, object closure, enum type equality, and unchanged legacy digests;
- sync, async, streaming, callback, meter, and lease failure paths;
- conservative unknown-outcome settlement, replay visibility, and error
  preservation;
- SQLite and PostgreSQL reservation, renewal, reconnect, duplicate retry,
  changed-charge rejection, and window accounting;
- deep-tree OpenTelemetry export, MCP result normalization, subtree verify and
  seal, and external seal custody;
- Python 3.10 and 3.13 CI endpoints, a local Python 3.12 gate, and a Python 3.14
  forward-compatibility check;
- PostgreSQL 14 through 18 using disposable test databases;
- wheel and source archive inspection, isolated wheel install, and offline
  evidence verification.

For 1.0.6, the offline suite passed on Python 3.10, 3.12, 3.13, and 3.14.
PostgreSQL 14 through 17 each passed the 97-test store acceptance set.
PostgreSQL 18 passed all 337 tests at 90.55 percent coverage. Ruff, strict
typing, the documentation scan, the offline EXP-006 verifier, strict artifact
metadata checks, archive inspection, and an isolated wheel smoke test also
passed.

## Separate cloud ledger

| Field | Value |
|---|---:|
| Authorization | 8.00 USD |
| Reserved | 0.00 USD |
| Settled | 0.00 USD |
| Ambiguous | 0.00 USD |
| Remaining | 8.00 USD |
| Paid requests | 0 |

The paid matrix is frozen at zero requests because the package changes are
covered by deterministic tests. Any later paid request requires a written row
with provider, model, purpose, maximum reservation, retry count, and remaining
capacity before dispatch. An ambiguous request is charged at its full reserved
ceiling.

## Remaining limits

- Pollard does not choose replacement models, split a provider tool set, or own
  an agent loop.
- Pollard cannot infer whether an arbitrary unmarked exception occurred before
  or after external dispatch.
- Conservative failure settlement can charge only meters with precheck
  estimates. A meter without an estimate cannot be reconstructed when the
  provider result is unavailable.
- Provider error bodies can contain sensitive data. They stay on the native
  exception and require caller-owned redaction and custody.
- If transactional settlement succeeds but node storage then fails, shared
  accounting can include a charge without a replayable result. Reconcile that
  reservation before considering another external request.
- A renewal callback that ignores its return path can outlive Pollard's bounded
  shutdown wait. The heartbeat is a daemon thread; database driver timeouts and
  worker-process supervision remain caller responsibilities.
- Failure-node metadata and reservation tables are mutable coordination data;
  subtree seals cover node identity and result digests, not those fields.
- External seal custody remains caller-triggered and requires separate access
  control, signing, and retention policy.
