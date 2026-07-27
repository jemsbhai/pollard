# Replay and live revalidation

Pollard separates deterministic replay from live provider revalidation.

- Strict replay returns the exact stored result and cannot call a model
  provider, step function, registered handler, or live policy hook.
- Live revalidation deliberately makes a new provider call, stores that result
  under a separate node, and compares it with the recording.

A new provider sample is never called "replay." Hosted inference may vary
because of sampling, model revisions, routing, safety systems, hidden provider
configuration, or numerical execution. Revalidation detects observable drift;
it cannot prove that a hosted provider preserved its internal execution state.

## Record an execution fingerprint

`ReplayContract` is a caller-declared execution fingerprint. Bind it before
recording when the known provider environment should be part of step identity:

```python
from pollard import ReplayContract, Runtime

recorded_contract = ReplayContract(
    provider="openai",
    model_revision="deployment-snapshot-2026-07",
    api_version="2026-06-01",
    adapter="pollard.adapters.openai.make_responses_fn",
    adapter_version="1",
    sdk="openai",
    sdk_version="2.12.0",
    application_revision="8f29c1a",
    environment={"region": "us-east"},
)
payload = recorded_contract.bind(
    {
        "model": "deployment-name",
        "input": "Summarize this text.",
        "temperature": 0,
        "max_output_tokens": 256,
        "_pollard": {"provider": "openai"},
    }
)
node = Runtime("runs.db").run("summary").model_call(payload, fn=provider_fn)
```

`bind()` returns a copy and preserves other fields in the reserved `_pollard`
object. Direct Pollard provider adapters remove `_pollard` before SDK dispatch.
Custom callables receive the caller's original payload and must handle reserved
metadata according to their own request contract.

The fingerprint is a declaration by the caller, not a provider signature or
attestation. Use precise deployment or model revision identifiers when the
provider exposes them. Put only stable, non-secret identity data in a contract.
Floats, credentials, timestamps, and ephemeral request IDs do not belong there.

A bound contract is part of node identity. Strict replay must therefore use the
recorded bound payload, and changing a bound field intentionally produces
`MissingRecording`. If an environment label should describe only a later live
observation and should not constrain replay identity, leave the original
payload unbound and pass the label through the revalidation `contract` only.

## Revalidate explicitly

Open the recorded run at its root in `record` mode and call
`revalidate_model_call`:

```python
from pollard import ReplayContract, Runtime

live_contract = ReplayContract(
    provider="openai",
    model_revision="deployment-snapshot-2026-08",
    application_revision="c4510de",
)
run = Runtime("runs.db", mode="record").run("summary")
report = run.revalidate_model_call(
    payload,
    fn=current_provider_fn,
    contract=live_contract,
    observation_id="release-candidate-2026-08",
)

print(report.matched, report.exact_match)
print(report.difference_paths)
```

By default the live callable receives the same payload that identifies the
recording. To compare against a deliberate model or request migration, pass a
separate `live_payload`:

```python
new_payload = live_contract.bind(
    {
        "model": "new-deployment-name",
        "input": "Summarize this text.",
        "temperature": 0,
        "max_output_tokens": 256,
        "_pollard": {"provider": "openai"},
    }
)
report = run.revalidate_model_call(
    payload,
    live_payload=new_payload,
    fn=current_provider_fn,
    contract=live_contract,
)
```

The first payload still derives the golden node; the new payload is committed
to the observation node, used by meters, and passed to the live callable. If it
contains a bound replay contract, that fingerprint must equal `contract`.

Preparation is fail-closed. Pollard first derives the expected golden node from
the current parent, payload, and attempt; verifies the recording and its
ancestry; and refuses a missing or damaged recording before dispatch. It also
rejects revalidation in `hybrid`, `replay`, and `dry_run` modes. An
`observation_id` that already exists at preparation time is rejected before a
second provider call. This existing-node check is not a distributed dispatch
lock; use the generated unique IDs and provider-supported idempotency controls
rather than coordinating concurrent workers through this field.

The live call follows the normal model-call path:

- budget and sliding-window prechecks run before dispatch;
- transactional reservations are acquired and renewed;
- measurement meters run;
- actual charges settle from the live result; and
- streams can use `on_delta` and `keep_chunks`.

The returned `RevalidationReport` contains node IDs, result digests, contracts,
match status, difference paths, and live charges. It does not duplicate the
recorded or live result values. Retrieve an authorized result through the
reported node ID when inspection is necessary.

After successful comparison, the run cursor advances along the golden branch,
not the observation branch. This permits sequential revalidation of a recorded
workflow while keeping every live observation as a side branch.

## Stored evidence

One successful revalidation adds two nodes without changing the golden node:

```text
recorded parent
├── golden model call
└── live revalidation model call
    └── immutable comparison note
```

The live model-call payload includes:

- a unique observation ID;
- the golden node ID and golden result digest;
- the named comparator; and
- the caller-declared live contract.

Its result is the complete live provider result and follows the same data
classification, retention, and redaction requirements as every model result.
The comparison note is value-free: it stores digests, boolean status, and JSON
Pointer paths such as `/text` or `/tool_calls/0/function/arguments/city`. It
never stores the differing values. A comparator exception produces a
content-free failure note with only the exception type.

Seal the subtree and retain the seal under independent custody when the
comparison evidence must be tamper-evident. Revalidation does not make the
underlying store tamper-proof.

## Comparators

The default `NormalizedModelComparator` compares stable model semantics:

- normalized `text`;
- normalized `tool_calls`, excluding provider-generated call IDs and indexes;
- `refusal`; and
- `structured_output`.

JSON-encoded tool arguments are parsed before comparison. Top-level usage,
provider usage, response IDs, models, finish reasons, metrics, and retained
stream chunks do not affect a semantic match when a normalized semantic field
is present. For an application-specific result without those fields, all
fields except `usage`, `provider_usage`, and `chunks` are compared.

`ExactResultComparator` compares the complete result. It is appropriate for
frozen fixtures and deterministic local inference, but hosted calls commonly
differ in response IDs or accounting even when their semantic output matches.

Applications can implement `RevalidationComparator`:

```python
from pollard import RevalidationComparison

class SchemaComparator:
    name = "my-application/schema-v1"

    def compare(self, recorded, live):
        matched = recorded.get("schema") == live.get("schema")
        paths = () if matched else ("/schema",)
        return RevalidationComparison(
            matched=matched,
            difference_paths=paths,
        )
```

Comparator names are retained in immutable evidence and should be versioned.
Custom comparators must return only value-free JSON Pointer paths. Pollard
cannot prevent caller-owned comparator code from encoding sensitive content in
those strings.

## Async parity

`AsyncRun.arevalidate_model_call` provides the same contract for async
functions and synchronous or asynchronous streams:

```python
report = await async_run.arevalidate_model_call(
    payload,
    fn=async_provider_fn,
    contract=live_contract,
)
```

Store operations remain synchronous, matching the rest of `AsyncRuntime`.

## Interpretation

`matched=True` means the selected comparator accepted this one live
observation. It does not prove future provider determinism, model quality,
factual correctness, or hidden-environment equality. Use repeated observations
and application-specific statistical evaluation for distributional claims.

For exact live regeneration, use self-hosted inference with pinned weights,
tokenizer, inference engine, quantization, decoding parameters, random seeds,
and runtime configuration. Even then, retaining and replaying the recorded
result is a stronger reproducibility boundary than assuming regeneration.
