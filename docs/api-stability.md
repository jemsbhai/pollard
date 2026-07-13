# API stability policy

Pollard 0.9 is the review candidate for this policy. The compatibility promise
starts with 1.0.0; prerelease versions remain free to correct the candidate
before that release.

## The 1.0 covenant

The following four surfaces are frozen from 1.0.0 through every 1.x release.
Changing any one incompatibly requires 2.0.

### Node identity

A node ID is lowercase hexadecimal SHA-256 over this exact byte sequence:

```text
b"pollard/v1\n" + canonical_bytes(identity_document)
```

The identity document has the exact keys below. Canonical serialization sorts
them, so their presentation order here is descriptive:

```json
{"a": attempt, "k": kind, "p": parent_id_or_empty_string, "pl": payload}
```

`a` is the integer attempt, `k` is the node-kind string, `p` is the parent node
ID or `""` for a root, and `pl` is the identity payload. Results and mutable
metadata are excluded.

### Canonical identity serialization

`canonical_bytes` validates an identity value, then emits UTF-8 JSON with keys
sorted, separators `,` and `:` with no added whitespace, and non-ASCII text
preserved rather than escaped.

Allowed values are null, strings, booleans, integers, lists of allowed values,
and objects whose keys are strings and whose values are allowed. Floats, bytes,
non-string object keys, and other Python values are rejected. In particular,
booleans remain booleans even though Python treats `bool` as an integer subtype.

### Store protocol

`pollard.Store` is the public structural protocol. Its required methods are:

```python
def put(self, node: Node) -> None: ...
def get(self, node_id: str) -> Node: ...
def exists(self, node_id: str) -> bool: ...
def children(self, node_id: str) -> list[str]: ...
def update_meta(self, node_id: str, patch: dict[str, object]) -> None: ...
def walk(self, root_id: str) -> Iterator[Node]: ...
def roots(self) -> list[str]: ...
```

`put` is append-oriented, requires an existing parent for a non-root node, and
is idempotent for the same node identity. `get` returns the named node and
raises `KeyError` when absent. `exists` is the corresponding presence check.
`children` returns direct child IDs in deterministic `(kind, id)` order.
`update_meta` applies a shallow metadata patch without changing node identity.
`walk` yields the root and then each descendant in deterministic child order.
`roots` returns deterministic root IDs ordered by run label and ID.

Backend-specific transactional, merge, retention, and compaction capabilities
remain outside this minimal protocol. Adding another required method is a
breaking protocol change.

### Step-function contract

A synchronous model or unfenced tool function receives the identity payload as
a dictionary. It returns either a result dictionary or an iterator of chunk
dictionaries. An asynchronous function returns or resolves to a result
dictionary, a synchronous iterator of chunk dictionaries, or an asynchronous
iterator of chunk dictionaries.

For a stream, every item must be a dictionary. A chunk containing `result`
replaces the accumulated result and that value must be a dictionary. A chunk
containing `delta` recursively merges that dictionary. A chunk containing
neither merges the chunk itself. Nested dictionaries merge, strings concatenate,
lists append, and other values replace. `keep_chunks=True` also stores the
ordered raw chunks under `result["chunks"]`.

The function runs only after policy and budget prechecks, and only for record
mode or a hybrid cache miss. Charges settle once after successful consumption.
Replay does not call the function.

## Other public APIs

All other documented public APIs follow Semantic Versioning after 1.0. A 1.x
release may add optional parameters, new classes, new enum members where callers
are not required to exhaustively match them, and optional backend capabilities.
It does not remove or incompatibly reinterpret a documented public name.

Private names beginning with an underscore, raw database schemas, mutable node
metadata fields, CLI human-readable formatting, and optional provider SDK
response fields are not frozen. Documented JSON CLI fields and on-disk
compatibility still follow normal Semantic Versioning.

## Deprecation policy

Except for the four frozen surfaces, a public API scheduled for removal will:

1. be marked deprecated in documentation and the changelog;
2. emit a `DeprecationWarning` where a runtime warning is practical;
3. name the supported replacement; and
4. remain available for at least one minor release and 90 days, whichever is
   longer.

Removal then occurs in the next eligible major release. A security or data
integrity defect may require a faster change. Such an exception must be limited
to the affected surface and documented in the release notes with migration
guidance.
