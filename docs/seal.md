# Seal Design

`seal()` creates an export digest for a Pollard subtree. It is not a new node id
scheme. Node ids still bind control-flow identity, and result digests still bind
stored result JSON. A seal chains those facts into one digest that can travel
with an exported run.

The traversal order is `Store.walk(root_id)`. Stores must make that order
deterministic. The built-in stores already sort children by kind and id, so the
same subtree produces the same seal.

For each visited node, Pollard first checks that the node id matches its identity
fields and that any stored result digest matches the stored result text. It then
hashes a canonical record with these fields:

- `index`: zero-based traversal index.
- `node_id`: the Pollard node id.
- `parent_id`: the parent node id, or an empty string for a root.
- `kind`: the node kind.
- `result_digest`: the stored result digest, or an empty string when the node has
  no result.
- `previous`: the prior seal hash, or an empty string for the first node.

The entry hash is `sha256("pollard/v1:seal\n" + canonical_record_bytes)`. The
last entry hash is the report digest.

The seal excludes mutable metadata by design. Metadata can record timings,
budget charges, meter snapshots, or later annotations. Those fields are useful
for inspection, but they are not part of the exported result chain.
