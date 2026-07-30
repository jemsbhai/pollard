# Seal design

`seal()` creates a deterministic integrity report for one Pollard subtree. It
is not a new node-ID scheme, a digital signature, encryption, an access-control
mechanism, or proof that a model answer is correct. Node IDs bind control-flow
identity, result digests bind stored result JSON, and a seal chains those facts
in traversal order so one digest can travel with an exported run.

## Create a seal

From Python:

```python
from pollard import Runtime, seal

runtime = Runtime("runs.db")
with runtime.run("audit") as run:
    run.note({"status": "ready"})
    report = seal(run.store, run.root_id)

print(report.algorithm)
print(report.digest)
print(report.to_dict())
```

From the CLI:

```powershell
pollard seal runs.db <root-id>
pollard seal runs.db <root-id> --json
pollard seal runs.db <root-id> --output seal.json --json
```

The short human-readable form is suitable for copying a digest. `--output`
writes the complete report, including every entry, for independent inspection.
Keep the expected final digest in a system controlled separately from the
Pollard store or exported subtree when later tamper detection matters.

## Validation before hashing

For every visited node, Pollard first checks:

1. Recomputing the node ID from its parent, kind, attempt, and payload produces
   the stored node ID.
2. When result text is present, recomputing its result digest produces the
   stored result digest.

Any mismatch raises `IntegrityError`; Pollard does not issue a seal over known
invalid data. A successful seal therefore covers validated identities and
result digests in the subtree present at that moment.

When the CLI seals MongoDB, traversal spans multiple snapshot transactions
rather than one point-in-time snapshot. Quiesce writes or otherwise stabilize
the namespace when the seal must represent one evidence boundary.

Neo4j CLI reads are write-routed to a primary but use read-committed isolation,
not snapshot isolation. A command can span multiple managed transactions.
Quiesce writes or otherwise stabilize the logical store when a Neo4j seal must
represent one evidence boundary.

Kafka CLI construction captures the exclusive high watermark, fully replays
the committed prefix `[0, high)`, and freezes that in-memory view. A seal is
therefore internally consistent while writers append, but it covers only that
captured prefix. Reconnecting captures a new prefix. Quiesce writers only when
the seal must represent a drained final topic, and independently record the
topic, partition zero, low watermark zero, and exclusive high watermark.

Run `pollard verify runs.db <root-id>` when a findings-oriented integrity report
is more useful than an exception. Run `pollard seal` when a deterministic
transfer digest is needed.

## Traversal and record format

The traversal order is `Store.walk(root_id)`. Stores must make that order
deterministic. The built-in stores visit the root first and sort each node's
children by `(kind, id)`, so the same subtree produces the same seal across
built-in backends after canonical rehydration.

For each visited node, Pollard hashes a canonical record with these fields:

- `index`: zero-based traversal index.
- `node_id`: the Pollard node ID.
- `parent_id`: the parent node ID, or an empty string for a root.
- `kind`: the node-kind string.
- `result_digest`: the stored result digest, or an empty string when no result
  is present.
- `previous`: the prior entry hash, or an empty string for the first node.

The entry operation is:

```text
sha256(b"pollard/v1:seal\n" + canonical_bytes(record))
```

The algorithm identifier reported by the API is
`sha256:pollard/v1:seal`. The final entry hash is `SealReport.digest`. An empty
subtree cannot occur because the requested root must exist.

Each full `SealEntry` reports the same identity fields plus its own `seal`
value. `SealReport` contains the requested `root_id`, algorithm identifier,
final digest, and ordered entries.

## What is covered

The chain covers, by reference:

- the exact root and descendant traversal order;
- every visited node ID, which commits to parent, kind, attempt, and payload;
- every visited result digest, which commits to its serialized result; and
- the presence and position of each visited node in that traversal.

Changing a payload, attempt, kind, parent, result, visited-node set, or traversal
order changes validation or the final digest. Appending a descendant after a
seal also changes the next seal because the visited-node set changes.

## What is excluded

Mutable metadata is excluded by design. It can contain timestamps, charges,
meter snapshots, labels, replay observations, merge conflict notes, or prune
markers. Those fields can change after the immutable semantic node is written.
As a consequence:

- a seal does not prove historical values of mutable charge or timing fields;
- a later metadata update does not invalidate the seal;
- a prune marker alone does not change the digest, although explicit garbage
  collection that removes the marked subtree does; and
- transactional budget reservations, settled arbitration rows, and sliding
  window events are not covered.

The seal also does not cover the database file as a byte-for-byte container,
indexes, SQLite page layout, PostgreSQL schema, encryption configuration,
backups, external attachments, provider logs, or the report file containing the
seal itself. For Kafka, it also excludes the topic name and configuration,
partition offsets and watermarks, event envelopes and operation ids, duplicate
commands, and metadata-patch history. Distinct valid Kafka logs can materialize
the same sealed Pollard tree.

## Export and import

`pollard export` writes a subtree plus a complete seal report:

```powershell
pollard export runs.db <root-id> subtree.json
pollard import subtree.json archive.db
```

Import parses every node, validates its identity and result digest, reconstructs
deterministic traversal, and compares the complete seal before writing any
node. A detached subtree whose root names an external parent can be imported
only when that parent already exists in the destination.

An export includes mutable metadata for utility, but the seal does not cover
that metadata. If metadata needs an immutable evidence boundary, serialize the
required fields in an application-owned signed manifest or record them as a new
Pollard note payload before sealing.

A Kafka export is a projection of the materialized subtree from the observer's
frozen prefix. It is not a broker-log backup and does not preserve the source
topic, offsets, command order, or operation history.

## Threat model and signatures

A stored seal beside its database detects accidental corruption only when the
attacker cannot replace both. For tamper evidence against a party who can
rewrite the store, save the expected digest in an independently controlled log,
object lock, transparency service, or signature envelope.

Pollard does not sign `SealReport.digest`. An application can sign the ASCII
hex digest together with the root ID, algorithm identifier, release version,
and context fields using its established signing system. Key creation,
rotation, timestamping, revocation, identity verification, and signature format
remain outside Pollard.

## Verification checklist

- Identify the exact store and root ID being sealed.
- Run `pollard verify` before export and stop on any finding.
- Generate the full seal report and record its algorithm and final digest.
- Store the expected digest outside the exported file when independence is
  required.
- Transfer the complete export rather than copying selected nodes by hand.
- Import into a fresh destination and compare the resulting root and digest.
- Treat metadata, provider logs, credentials, and attachments as separate
  evidence with their own controls.
- Generate a new seal after appending descendants or removing pruned subtrees.
