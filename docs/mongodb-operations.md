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

The constructor confirms a replica set or mongos response, opens
`<prefix>_records` and `<prefix>_coordinators`, and creates the unique
record index. It then initializes or validates Pollard schema version 1.

## Authentication And Least Privilege

Keep credentials in the application's secret manager or connection
environment, not in a Pollard payload or committed URI. Require TLS and verify
the server certificate. Scope the database user to the selected database and
collections.

The application role needs collection creation on first use when the
collections do not exist, index creation for `<prefix>_records`, and find,
insert, update, and remove access on both Pollard collections. An administrator
can create the collections and unique index first, after which the application
role can omit schema-creation privileges.

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

`reconnect()` builds a replacement MongoClient, confirms the topology,
creates or checks the index, and validates the existing Pollard schema before
closing the previous client. If replacement validation fails, the prior
validated client remains installed. Do not invoke reconnect concurrently on
the same store object.

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
