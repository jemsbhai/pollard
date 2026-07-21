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
| Operator interruption or task cancellation after dispatch released a reservation | Pollard correctness defect | Adapter and stream boundaries now treat every `BaseException` after dispatch as an unknown outcome, including `KeyboardInterrupt`, `SystemExit`, and async cancellation. |
| OpenAI Responses returned `failed` or `incomplete` terminal states | Pollard correctness defect | Failed states now raise a structured error with the raw terminal event; incomplete states retain their actual usage and partial result. |
| Anthropic or Bedrock cache-token fields were omitted from normalized input totals | Pollard correctness defect | Normalized input usage now includes cache creation/read or cache write/read tokens according to each provider contract. |
| A completed response omitted or corrupted provider usage | Pollard correctness defect | A meter with a precheck estimate settles that estimate conservatively and records the fallback instead of releasing it to zero. |
| A provider stream ended without its required terminal event | Pollard correctness defect | Direct adapters raise a structured unknown-outcome error instead of synthesizing an empty successful response. |
| Provider-native usage breakdowns were overwritten during normalization | Pollard hardening opportunity | Direct adapters retain a `provider_usage` snapshot beside normalized `usage`. |

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

Operator interrupts and asynchronous cancellation are included. Once dispatch
has begun, the accounting boundary catches `BaseException`, marks the outcome
unknown, performs bounded cleanup, and then re-raises the original exception.
Errors received as terminal stream events keep their provider-native detail on
the exception; failure nodes continue to store only the exception type and
content-free request digest.

When generation succeeds but normalized usage is missing or invalid, a meter
that made a precheck estimate keeps that estimate as the conservative settled
charge. The node records `accounting_fallbacks` so the estimate is auditable.
Valid provider usage remains authoritative. A meter that made no estimate still
cannot reconstruct an absent charge.

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

The 1.0.7 candidate passed all 315 applicable tests on Python 3.10 and 3.14.
The full Python 3.12/PostgreSQL 18 run passed 357 tests at 90.73 percent
coverage. PostgreSQL 14, 15, 16, 17, and 18 each passed all 25 store acceptance
tests. Ruff and strict typing pass for the complete package. Documentation,
artifact, isolated-install, and offline evidence gates run before release.

## Current account and serialization gates

The follow-up used only synthetic prompts and schemas. OpenAI model metadata
accepted `gpt-5.4-nano-2026-03-17`, and the Responses input-token endpoint
accepted the exact strict-tool request after Pollard expanded its local
reference (148 input tokens). Gemini model metadata accepted
`gemini-2.5-flash-lite` and `gemini-2.5-pro`.

The installed google-genai 1.62.0 SDK exposes `system_instruction` and `tools`
on `CountTokensConfig` but rejects `system_instruction` while serializing a
Gemini Developer API count request. The official REST `countTokens` shape using
`generateContentRequest` accepted the equivalent system instruction and tool
(108 input tokens). This remains an SDK/backend portability behavior and a
caller-owned serialization concern; it is not moved into Pollard core.

Anthropic and Bedrock live gates are skipped because this environment has no
corresponding credentials. Their behavior is covered by frozen fixtures and
official contracts.

## Frozen paid validation matrix

This matrix was frozen after every mocked, schema, serialization, PostgreSQL,
and account-access gate passed. Each row permits one request and zero retries.
No paper prompt, transcript, credential, or licensed task data is used.

| ID | Provider | Model | Purpose | Maximum output | Reservation ceiling | Retries |
|---|---|---|---|---:|---:|---:|
| C1 | OpenAI | `gpt-5.4-nano-2026-03-17` | Complete Responses stream with one forced strict function, expanded local reference, parallel calls disabled, normalized and native usage | 64 tokens | 0.05 USD | 0 |
| C2 | Google Gemini Developer API | `gemini-2.5-flash-lite` | Generation counterpart to the accepted REST count projection; one allowed function and cardinality audit | 64 tokens | 0.05 USD | 0 |
| C3 | Google Gemini Developer API | `gemini-2.5-pro` | Account-specific generation availability after metadata success | 16 tokens | 0.05 USD | 0 |

The total preregistered ceiling is 0.15 USD. A request with an ambiguous
outcome is charged at its full 0.05 USD ceiling. Clear validation or
availability errors are recorded without retry.

### Paid results

| ID | Result | Audit outcome | Ledger charge |
|---|---|---|---:|
| C1 | Completed; 148 input tokens, 24 output tokens, one function call | Strict resolved schema, single-call policy, terminal streaming event, normalized usage, and retained provider usage all passed | 0.00005960 USD |
| C2 | Provider code 503 after dispatch | SDK generation serialization passed its local boundary, but the external service did not complete the validation; outcome charged as ambiguous | 0.05000000 USD |
| C3 | Provider code 404 after metadata success | Reproduced the account-specific metadata-versus-generation availability class; clear availability failure | 0.00000000 USD |

All three preregistered requests were attempted once. No retry or unregistered
request was made. The Gemini 503 does not establish function-call cardinality;
that behavior remains covered by deterministic fixtures and caller enforcement.

## Separate cloud ledger

| Field | Value |
|---|---:|
| Authorization | 8.00 USD |
| Preregistered ceiling | 0.15 USD |
| Reserved after completion | 0.00 USD |
| Settled completed usage | 0.00005960 USD |
| Ambiguous (charged at ceiling) | 0.05000000 USD |
| Conservative total | 0.05005960 USD |
| Remaining authorization | 7.94994040 USD |
| Paid requests | 3 |
| Retries | 0 |

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
