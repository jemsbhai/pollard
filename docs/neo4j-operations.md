# Neo4j Store Operations

This guide is the production contract for `Neo4jStore`. Use the
[distributed-store guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
for cross-backend selection and shared uncertainty handling.

## Capability And Topology

Neo4jStore is a synchronous Store, TransactionalArbiter, and RenewableArbiter.
Each logical `store_id` has one coordinator node. Every write increments that
coordinator in a managed write transaction before reading mutable state, which
prevents read-modify-write loss. This deliberately serializes writes within one
logical store.

Neo4j Community Edition is a single-instance deployment. Clustering and online
backup are Enterprise Edition capabilities; Aura is the managed alternative.
The release matrix uses Community for routine transaction compatibility and a
three-primary Enterprise evaluation cluster for forced writer-loss acceptance.
That local cluster proves routing, election, reconnect, retry tombstones,
replay, verification, and external-seal behavior on one Docker host. It does
not prove arbitrary partition safety or independent failure-domain durability.

## Install And Connect

```powershell
python -m pip install "pollard[neo4j]"
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
$env:POLLARD_NEO4J_USER = "pollard_app"
$env:POLLARD_NEO4J_PASSWORD = "<secret>"
```

```python
import os
from pollard import Neo4jStore

store = Neo4jStore(
    os.environ["POLLARD_NEO4J_URI"],
    (
        os.environ["POLLARD_NEO4J_USER"],
        os.environ["POLLARD_NEO4J_PASSWORD"],
    ),
    database="neo4j",
    store_id="support-prod",
    connection_timeout=5,
    connection_acquisition_timeout=10,
    max_transaction_retry_time=15,
)
```

Use a `neo4j://`, `neo4j+s://`, or `neo4j+ssc://` URI when routing through
a cluster. The driver discovers database roles and directs Pollard's write-mode
sessions to a primary. A `bolt://` URI targets one machine and bypasses the
routing table; use it only when that is the intended topology. Prefer
`neo4j+s://` with CA-validated encryption for production.

For Python construction, additional keyword arguments pass to
`GraphDatabase.driver`. Advanced driver policy remains caller-owned, including
resolver, authentication manager, client certificate, pool sizing, telemetry
choice, acquisition timeout, and retry time.

With the default `create=True`, construction first classifies the logical
namespace without writing. An exact existing schema and coordinator must also
have both exact named uniqueness constraints and their exact owned, online
range indexes; Pollard does not repair them. Only a completely fresh namespace
proceeds to create or confirm those
database-global constraints, then initializes schema version 1 and coordinator
revision 1 together in one managed data transaction. Constraint DDL is outside
that logical transaction.

Pass `create=False` to verify connectivity and require the exact existing
schema, coordinator, constraints, and backing indexes without creating nodes,
properties, constraints, indexes, or schema records. Schema-only,
coordinator-only, data-only, wrong-identity, future-version, and partial,
incompatible, or offline constraint/index states all fail closed.

## CLI Access And Merge Destinations

The CLI accepts Neo4j only through an environment-backed URI and two required
basic-auth references:

```powershell
$env:POLLARD_NEO4J_URI = "neo4j+s://graph.example"
$env:POLLARD_NEO4J_USER = "pollard_reader"
$env:POLLARD_NEO4J_PASSWORD = "<secret>"
$neo4jStore = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=neo4j#support-prod"
pollard runs $neo4jStore --json
pollard show $neo4jStore <root-id>
pollard report $neo4jStore <root-id> --json
pollard verify $neo4jStore --json
pollard seal $neo4jStore <root-id> --output seal.json --json
pollard export $neo4jStore <root-id> subtree.json --json
pollard merge combined.db $neo4jStore --json
pollard merge $neo4jStore runs.db --json
$neo4jArchive = "neo4j-env:POLLARD_NEO4J_URI?user-env=POLLARD_NEO4J_USER&password-env=POLLARD_NEO4J_PASSWORD&database=pollard_archive#archive-2026-07"
pollard merge $neo4jArchive runs.db --initialize-if-missing --json
```

The grammar is
`neo4j-env:URI_VAR?user-env=USER_VAR&password-env=PASSWORD_VAR&database=DB#store-id`.
Both auth references are required. Database and store id default to `neo4j` and
`default` for sources and must match the writer configuration. Every
destination must explicitly provide the URI, user, and password environment
references, database, and store id. CLI labels show the URI reference,
database, and store id, but omit both auth references and all environment
values.

Direct `neo4j://`, `neo4j+s://`, `neo4j+ssc://`, `bolt://`, `bolt+s://`, and
`bolt+ssc://` arguments are rejected. Keeping connection and auth values in the
environment keeps them out of Pollard process arguments. Inject production
values with a secret manager or another mechanism that does not record the
assignment itself in shell history.

Sources and ordinary destinations construct `Neo4jStore(create=False)`. An
unused database/store-id combination, missing coordinator, or missing or
changed constraint or backing index therefore fails closed without writes or
DDL.
`--initialize-if-missing` is valid only for an explicit `neo4j-env:`
destination. Pollard may validate that selector and flag combination first
without reading the referenced environment values. After every source has been
fully traversed and validated into a private disk-backed spool, closed, and
every finalized spool has passed validation, that opt-in reads the destination
environment values and constructs it with `create=True`. Review every
reference, database, and store id first: a typo can otherwise create a
different fresh logical namespace.

Fresh construction classifies logical and physical state before constraint
DDL. It creates both shared database-wide constraints in one schema
transaction, or validates the exact existing pair and their backing indexes,
outside the logical transaction. It then creates the coordinator and schema
together in one managed data transaction. A later failure can therefore leave
only the global constraints and indexes, which may also serve other Pollard
stores. Do not delete shared constraints as automatic cleanup. Partial or
ambiguous logical states and partial,
incompatible, alternate-name, or offline constraint/index states are never
repaired implicitly.

Reads remain write-routed to a primary, including on a cluster. Neo4j uses
read-committed isolation, and one CLI command can span several managed
transactions rather than one command-wide snapshot. Merge destination
application is also node-by-node, not one cross-node transaction. A failure can
retain accepted nodes or metadata; repeating the exact merge is idempotent.
Quiesce source and destination writers, inspect and rerun after a failure,
verify the destination, and seal required roots. Concurrent destination writers
can race mutable merge metadata.

Preparation uses bounded working memory and temporary disk proportional to all
source trees. Private spools contain full node content and cleanup is attempted
on every exit. Cleanup failure can leave private artifacts and makes an
otherwise successful command fail. Combined failures preserve their primary
attribution and add a fixed cleanup notice. Creation, serialization,
disk-write, finalization, corruption, or truncation fail before destination
access. After application, accepted Neo4j nodes or metadata remain and an exact
rerun is safe.

Neo4j supports `show`, `report`, `verify`, `seal`, `export`, and `runs`, and can
be a `merge` source or an environment-backed merge destination. It is not an
import or garbage-collection target. The CLI constructs basic tuple auth only.
Use the Python API for authentication managers, bearer or Kerberos auth, custom
resolvers, client certificates, impersonation, pool tuning, telemetry choice,
and other advanced driver configuration.

## Schema And Least Privilege

Pollard creates two uniqueness constraints:

- `pollard_neo4j_kv_record_key` on
  `_PollardKV(record_key)`; and
- `pollard_neo4j_coordinator_key` on
  `_PollardCoordinator(coordinator_key)`.

The constraints and their owned range indexes are global to the selected
database and shared by all Pollard logical store ids. Only a fresh
`create=True` construction attempts their
creation. That identity needs permission to create constraints and to match,
create, update, and delete Pollard-labeled nodes. An administrator can create
the exact constraints first and then remove schema-write permission from the
application role.

An observational or existing-only CLI role needs access to the selected
database, `MATCH` privilege for all properties on `_PollardKV` and
`_PollardCoordinator` nodes, and permission to inspect constraint and index
metadata with `SHOW CONSTRAINTS` and `SHOW INDEXES`.
Write routing selects a primary but does not replace Neo4j authorization.
Existing-only construction needs no node-write or constraint-create privilege.
A merge destination additionally needs permission to create and update Pollard
nodes. Fresh initialization also needs constraint-create privilege unless an
administrator has already installed the exact shared constraints.

Do not grant the application permission to drop constraints, drop the database,
or modify unrelated labels. `store_id` is a logical key, not an authorization
boundary. Use database, role, network, and encryption boundaries for tenant
isolation.

## Managed Retries And Timeouts

Neo4j managed transaction functions can be executed more than once after
retryable failures. Pollard's function contains only deterministic graph state
transitions. Never place a provider request or external side effect inside it.

Set finite connection, acquisition, and `max_transaction_retry_time` values
that fit the application's deadline and reservation lease. The database's
transaction timeout must also bound server work. Account for routing refresh,
leader election, TLS, and worker scheduling.

Pollard handles a connection failure that escapes the driver by constructing a
new driver and retrying the same reservation identity once. Repeated failure
becomes explicit reservation or settlement uncertainty.

## Reconnect And Routing

`reconnect()` creates a new driver and validates the exact existing schema,
coordinator, and constraints before replacing the previous driver. Reconnect
never issues constraint DDL or repairs state, even when the object was
originally constructed with `create=True`. All modes fail closed on missing,
partial, or changed state. Do not call reconnect concurrently with operations
on the same Neo4jStore object.

All sessions use write routing, including structural reads. This avoids serving
a follower view immediately after a Pollard commit. It also means read capacity
and availability follow the primary path, not a read-replica path.

For cluster entry, prefer a discoverable DNS name with several addresses or a
caller-owned custom resolver. Connecting through only one seed is less
available when that seed is unreachable before the routing table is obtained.
`reconnect()` constructs a new driver from the original URI, so at least one
address produced by that URI or resolver must still be a reachable router.
Allow the connection-acquisition and transaction-retry deadlines to cover
routing refresh and leader election; a list that includes live members does
not make an unrealistically short deadline reliable.

## Monitoring

Alert on:

- database or primary unavailability and routing-table refresh failures;
- loss of a majority of primary allocations;
- managed transaction retries and retry-time exhaustion;
- connection pool acquisition timeout and pool saturation;
- transaction lock wait, deadlock, and coordinator hot-spot latency;
- constraint or schema changes;
- store node count, database growth, checkpoint, and backup failures;
- reservation uncertainty and lease loss; and
- TLS, authentication, or certificate rotation failures.

Use transaction metadata or driver logging only with content-free identifiers.
Do not log Cypher parameters containing Pollard node records, results, or
credentials.

## Backup And Restore

Use Aura snapshots or the supported Enterprise backup tools for an online
production backup. For Community development data, stop the server before a
filesystem copy and follow Neo4j's version-specific restore instructions.
Never treat cluster replication as an independent backup.

For restoration:

1. Stop new provider and tool dispatch and reconcile reservations.
2. Retain external seals under different credentials.
3. Restore into a separate database or DBMS.
4. Open Neo4jStore with the same database and `store_id`.
5. Confirm both constraints and Pollard schema version 1.
6. Run `verify()`, compare external seals, and strictly replay representative
   runs.
7. Inspect shared counters, retry tombstones, leases, and windows.
8. Move traffic only after the recovery point is accepted.

`merge()` transfers tree nodes, not live governance state.

## Credential Rotation

Create a replacement role, password, bearer token, or client certificate first.
Verify it with a new driver and Neo4jStore, then perform a drained application
handoff. Authentication-manager callbacks must not call back into the driver.
Revoke the old identity only after store verification and external seal
comparison.

## Production Acceptance

Before enabling provider traffic, test:

- exact contention through at least two application clients;
- primary failover during read, reserve, settle, and renewal;
- routing refresh when one advertised server is unavailable;
- duplicate and changed reservation retries;
- finite retry exhaustion during a partition;
- process restart and same-object reconnect;
- role, password, and certificate rotation;
- backup restore to a separate target; and
- strict replay, `verify()`, and external seal comparison.

The 1.1.1 release matrix includes a local Neo4j 5.26 Enterprise evaluation
cluster with three primary allocations. The exact writer was killed, a routed
write completed after election, `reconnect()` used a surviving seed, unchanged
settlement replay remained idempotent, changed charges failed closed, and
strict replay, verification, and external-seal custody passed. Production
deployments must still run the remaining topology-, network-, TLS-, backup-,
and credential-specific checks above.

Official references: [Neo4j Python connections](https://neo4j.com/docs/python-manual/current/connect/),
[advanced connection configuration](https://neo4j.com/docs/python-manual/current/connect-advanced/),
[managed transactions](https://neo4j.com/docs/python-manual/current/transactions/),
[cluster routing](https://neo4j.com/docs/operations-manual/current/clustering/setup/routing/),
and [Neo4j edition boundaries](https://neo4j.com/docs/operations-manual/current/introduction/).
