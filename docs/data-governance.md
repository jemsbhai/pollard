# Data Governance

Pollard records an execution tree. Retaining that tree is a data-governance
decision, not only a storage decision. This document states what reaches the
ledger, which fields are integrity-protected, and which operator actions remove
data.

## What The Ledger Stores

Each node stores:

- Identity fields: parent id, kind, attempt, and the identity payload.
- Result fields: the serialized result and its SHA-256 digest.
- Mutable metadata: timestamps, charges, usage, labels, replay markers, and the
  pruned marker.

The node id commits to the full rehydrated identity payload. The result digest
commits to the serialized result. Mutable metadata is intentionally outside both
commitments.

By default, payload and result content is stored exactly as supplied. The
content-free CLI and HTML defaults prevent accidental display, but they do not
change what is at rest. `--payloads` displays every non-redacted payload and
result value.

## Interning Is Not Redaction

`SQLiteStore` and `PostgresStore` intern payload string leaves whose UTF-8 form
is at least 1 KiB. The threshold is configurable with `intern_threshold`, and
interning can be disabled with `intern_payloads=False`.

An interned string remains plaintext in the `blobs` table, stored once and
referenced from node payload rows. This reduces duplicate storage. It is not
encryption, redaction, or an access-control boundary.

Interning does not change node identity. Pollard computes the id from the full
payload before the store replaces large string leaves with internal references.
`get()` restores the full payload before returning a node. Stores with
interning on and off return the same canonical payload bytes and node ids.

## Redact Before Hash

`redact(value, hint=None)` returns a marker containing a domain-separated
SHA-256 digest and an optional hint. Pollard hashes and stores that marker, not
the source value.

```python
from pollard import redact

audit_value = redact(customer_reference, hint="customer reference")
run.note({"customer": audit_value})
```

The digest proves whether a later candidate value matches the committed value.
It does not permit recovery from Pollard. It is also not encryption. A person
who can guess a low-entropy value can hash guesses and compare them with the
marker. Use high-entropy values, or apply a keyed digest in your application
before calling `redact()`, when guessing is a concern. Hints are stored in
plaintext and must not contain secret content.

Redaction changes the identity payload by design. Replay can match when the
caller supplies the original value again and Pollard computes the same marker.
Pollard cannot reconstruct the original value from a stored marker.

## Sensitive Registry Fields

An `ActionSpec` string property may set `sensitive: true`:

```python
schema = {
    "type": "object",
    "properties": {
        "token": {"type": "string", "sensitive": True},
        "message": {"type": "string"},
    },
    "required": ["token", "message"],
    "additionalProperties": False,
}
```

The runtime validates the original arguments, passes them to policies, and
passes them to the registered handler only when execution is allowed. Before
creating an audit node, it replaces each sensitive string with a redaction
marker. Policy refusals, confirmation records, replay identities, and dry-run
nodes use the redacted form.

Unknown actions have no trusted schema. Pollard cannot infer which of their
arguments are sensitive. Callers must apply `redact()` before submitting values
that could appear in an unknown-action refusal.

Handler results and metadata are outside the sensitive-field transform. A
handler that returns its input token will place that token in the result. Keep
secrets out of handler return values and caller-supplied metadata.

## Retention And Garbage Collection

Pollard never removes nodes implicitly. `run.prune()` marks the current node;
it does not delete it. An operator must invoke an offline operation explicitly:

```powershell
pollard gc runs.db drop-pruned
pollard gc runs.db compact
```

`drop-pruned` verifies the existing trees, deletes each marked node and its
descendants, then seals every surviving root. The report lists removed node ids
and survivor seal digests.

`compact` removes unreferenced interned blobs and asks SQLite to vacuum the
file. It does not remove tree nodes. Running it after `drop-pruned` reclaims blob
space formerly used only by deleted subtrees.

Transactional stores also keep mutable budget reservations, settled budget
state, and sliding-window events. These rows are coordination state, not node
identity fields, and subtree seals do not cover them. Backups intended for live
continuation must include those tables. A sealed export remains sufficient for
offline tree verification but does not restore active governance windows.

The operation is offline by contract. Do not run it while another process is
writing the same store.

## Sealed Export And Import

```powershell
pollard export runs.db <root-id> subtree.json
pollard import subtree.json archive.db
```

An export contains canonical payload text, exact serialized results, metadata,
and a rolling subtree seal. Import parses and verifies every identity and result
digest, reconstructs the deterministic tree order, and compares the complete
seal report before writing any node.

Mutable metadata is carried by the export but is not covered by the seal. Keep
an exported seal digest in an independently controlled record when later
tamper detection matters. A detached subtree whose root has an external parent
can be imported only when the target already contains that parent.

## Operator Checklist

- Classify payload, result, and metadata fields before recording production
  calls.
- Mark registry string fields `sensitive: true` where plaintext is unnecessary
  for audit.
- Keep secrets out of results, metadata, labels, run names, and redaction hints.
- Restrict filesystem and database access independently of Pollard.
- Define a retention period and schedule explicit `drop-pruned` and `compact`
  operations.
- Save export seal digests outside the exported file when independent evidence
  is required.
