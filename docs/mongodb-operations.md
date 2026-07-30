# MongoDB Store Operations

This guide is the production contract for `MongoStore`. Use the
[distributed-store guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
for cross-backend selection and shared uncertainty handling.

## Capability And Topology

MongoStore is a synchronous Store, TransactionalArbiter, and RenewableArbiter.
It uses snapshot transactions, majority write concern, primary reads, and one
coordinator document per logical `store_id`. Decimal accounting values are
stored as strings.

MongoStore requires a replica set or sharded deployment and refuses a
standalone server. A single-member replica set is useful for development only.
Production fault tolerance normally requires at least three data-bearing
replica-set members on separate failure domains.

## Install And Connect

```powershell
python -m pip install "pollard[mongodb]"
$env:POLLARD_MONGODB_URI = "mongodb://pollard_app:password@db-a.example,db-b.example,db-c.example/pollard?replicaSet=rs0&tls=true&retryWrites=true&timeoutMS=10000"
```

```python
import os
from pollard import MongoStore

store = MongoStore(
    os.environ["POLLARD_MONGODB_URI"],
    database="pollard",
    store_id="support-prod",
    collection_prefix="pollard",
)
```

Additional keyword arguments pass to `pymongo.MongoClient`. Use them for a
caller-owned Stable API object, CA file, compressors, application name, and
timeout policy. Prefer a discoverable multi-host or `mongodb+srv` URI in
production. `directConnection=true` is intended for isolated development
topologies and disables normal member discovery.

With the default `create=True`, the constructor confirms a replica set or
mongos response and classifies the exact logical namespace before writing.
For a fresh namespace it creates or validates the unique record index, then
initializes schema version 1 and the coordinator identity/revision in one
MongoDB transaction. Index DDL is a separate physical operation and is not
part of that logical transaction; an empty records collection and index can
remain if later initialization fails. A retry may reuse that unambiguous empty
shell. Schema-only, coordinator-only, data-without-schema, malformed
coordinator, and incompatible-index states fail closed without repair.

Pass `create=False` to validate the topology, schema, coordinator, and unique
record index without creating collections, indexes, coordinator state, or
schema records. Reconnect always uses this non-mutating validation path.

## CLI Access And Merge Destinations

The CLI accepts MongoDB only through an environment-backed URI specification:

```powershell
$env:POLLARD_MONGODB_URI = "mongodb://pollard_app:password@db-a.example,db-b.example,db-c.example/pollard?replicaSet=rs0&tls=true&retryWrites=true&timeoutMS=10000"
$mongoStore = "mongo-env:POLLARD_MONGODB_URI?database=pollard&prefix=pollard#support-prod"
pollard runs $mongoStore --json
pollard show $mongoStore <root-id>
pollard report $mongoStore <root-id> --json
pollard verify $mongoStore --json
pollard seal $mongoStore <root-id> --output seal.json --json
pollard export $mongoStore <root-id> subtree.json --json
pollard merge combined.db $mongoStore --json
pollard merge $mongoStore local-recording.db --json
$mongoArchive = "mongo-env:POLLARD_MONGODB_URI?database=pollard_archive&prefix=pollard_import#archive-2026-07"
pollard merge $mongoArchive local-recording.db --initialize-if-missing --json
```

The grammar is
`mongo-env:VARIABLE?database=DATABASE&prefix=PREFIX#store-id`. Database,
collection prefix, and store id default to `pollard`, `pollard`, and `default`
for sources. A mutating destination must explicitly provide all three; the
database is not inferred from the URI path. CLI output uses the
environment-variable name and selector values, never the URI. Direct
`mongodb://` and `mongodb+srv://`
arguments are rejected because a URI on the command line can expose credentials
to process inspection and shell history.

Sources and ordinary destinations construct `MongoStore(create=False)`. An
unused or mistyped database/prefix/store-id combination therefore fails closed
without creating collections, indexes, coordinator state, or schema records.
`--initialize-if-missing` is the only explicit opt-in to a new destination.
Pollard may validate the destination selector and flag combination first, but
does not read its environment value or construct a client. It fully traverses
and validates every source into a private disk-backed spool, closes each source,
and validates all finalized spools before resolving the destination environment
variable or constructing its client. Topology validation still requires a
replica set or mongos.

MongoDB is supported as an observational/read source for `show`, `report`,
`verify`, `seal`, `export`, and `runs`, and as a `merge` source or destination.
`import` and `gc` remain SQLite-only. Each individual read uses a snapshot
transaction, but a CLI traversal spans multiple transactions and is not one
point-in-time snapshot. Destination application is also node-by-node rather
than one all-node transaction. Quiesce both source and destination writers;
after success or partial failure, repeat the exact merge if needed, run
`verify`, and seal required roots. Exact reruns are idempotent, but concurrent
destination writers can race merge-metadata updates. Prepared plans for all
sources are retained as private disk-backed spools rather than in aggregate
RAM. Temporary disk must hold all prepared data. Spools contain full node
content and cleanup is attempted on every exit. Cleanup failure can leave
private artifacts and makes an otherwise successful command fail; combined
failures preserve their primary attribution and add a fixed cleanup notice.
Creation, serialization, disk-write, finalization, corruption, and truncation
fail before destination access. Cleanup failure after application does not roll
back accepted MongoDB writes.

This CLI path constructs an ordinary URI-backed PyMongo client. Use the Python
API when the deployment requires a caller-owned Stable API object, CA file,
compressors, application name, timeout policy, or another option that cannot be
represented in the URI.

## Authentication And Least Privilege

Keep credentials in the application's secret manager or connection
environment, not in a Pollard payload or committed URI. Require TLS and verify
the server certificate. Scope the database user to the selected database and
collections.

The application or initializing CLI role needs collection creation on first use when the
collections do not exist, index creation for `<prefix>_records`, and find,
insert, update, and remove access on both Pollard collections. An administrator
can create the collections and unique index first, after which the application
role can omit schema-creation privileges.

An observational or existing-only CLI role needs connection and
transaction-session access, `find` on both Pollard collections, and permission
to list indexes on `<prefix>_records`. Existing-only construction does not call
`createIndex` and does not write coordinator or schema state. Normal
destination merging additionally needs insert and update access.

`store_id` is logical record isolation, not authorization. Use separate
databases, users, encryption keys, and network policies when tenants require an
access boundary.

## Timeouts And Driver Retries

Set a finite `timeoutMS` or equivalent server-selection, connect, and socket
timeouts. The total must fit the application deadline and reservation lease.
Also bound the transaction commit time according to the deployment's latency
and failover target.

PyMongo `with_transaction()` can retry the transaction callback or commit.
Pollard's callback contains only deterministic database state transitions and
is safe to repeat. Never move a provider request, tool side effect, message
send, or mutable application callback into MongoStore's transaction.

If the driver still reports a connection failure at Pollard's boundary,
Pollard reconnects and retries the same reservation identity once. A repeated
failure is explicit uncertainty, not evidence that the first transaction was
absent.

## Reconnect And Topology Change

`reconnect()` builds a replacement MongoClient, confirms the topology, and
validates the existing Pollard schema, coordinator, and exact unique index
before closing the previous client. It never creates or repairs them, even
when the object was originally opened with `create=True`. If replacement validation
fails, the prior validated client remains installed. Do not invoke reconnect
concurrently on the same store object.

A successful reconnect does not prove that every replica contains the latest
acknowledged write. Use majority write concern, monitor majority commit lag,
and test the deployment's election and rollback policy.

## Monitoring

Alert on:

- loss of primary, majority, or required replica-set members;
- replication and majority commit lag;
- transaction abort, retry, lifetime-limit, and unknown-commit-result rates;
- server-selection, network, socket, and write-concern timeouts;
- rollback events and storage or journal errors;
- collection and index growth;
- reservation uncertainty, lease loss, and integrity errors; and
- a change in replica-set name, sharded-router identity, or TLS certificate.

Tag driver telemetry with a content-free application name. Do not log the full
URI, command values, stored node bodies, or credentials.

## Backup And Restore

Use an Atlas point-in-time restore or a deployment backup method that produces
a transactionally consistent copy of both Pollard collections. Replica
membership is availability, not an independent backup.

For planned backup and migration:

1. Stop new provider and tool dispatch.
2. Drain or explicitly reconcile active reservations.
3. Record external seals for required roots.
4. Create and checksum the backup under separate credentials.
5. Restore into a separate database or cluster.
6. Open MongoStore with the same prefix and `store_id`.
7. Run `verify()`, compare external seals, and strictly replay representative
   runs.
8. Inspect shared budgets, retry tombstones, leases, and window events before
   cutover.

Do not use `merge()` as a live-governance migration. It copies node trees but
not active reservations or shared counters.

## Credential Rotation

Create the new MongoDB user or certificate first, test it with a new
MongoStore, update the caller-owned URI or options, and replace the application
store during a drained handoff. Revoke the old identity only after
verification. Keep the database, collection prefix, and `store_id` unchanged.

## Production Acceptance

Before enabling provider traffic, test:

- concurrent reservations through at least two application clients;
- forced primary election during reads, reserve, settle, and renewal;
- duplicate reserve and settle with identical and changed inputs;
- finite timeout behavior during a partition;
- process restart and same-object reconnect;
- credential and certificate rotation;
- isolated point-in-time restore;
- strict replay, `verify()`, and external seal comparison; and
- a rollback scenario consistent with the declared recovery objective.

Official references: [PyMongo connection targets](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-targets/),
[PyMongo transactions](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/),
[client timeouts](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/),
and [production transaction considerations](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/).
