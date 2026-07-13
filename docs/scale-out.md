# Scale-Out Stores And Governance

Pollard 0.8 adds a shared-arbiter path for worker teams. PostgreSQL coordinates
multiple hosts. SQLite uses the same transaction contract for processes sharing
one database file on one host. MemoryStore and HashRopeStore remain local
backends and use the earlier per-runtime budget checks.

This design has one hard boundary: all workers governed by one shared limit
must use the same transactional store and logical store id. Pollard is not a
consensus system and does not coordinate disconnected databases.

## Install And Connect

Install psycopg 3 through the optional extra:

```powershell
pip install "pollard[pg]"
```

Keep the connection string outside source control:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
```

```python
import os

from pollard import PostgresStore

store = PostgresStore(
    os.environ["POLLARD_PG_DSN"],
    store_id="support-prod",
)
```

The DSN supplies the database hostname, port, database, user, password, and any
TLS parameters required by the operator. Pollard does not read model-provider
credentials. A database role needs these privileges:

- Connect to the selected database and use the target schema.
- Create tables, indexes, and sequences on first use.
- Select, insert, update, and delete rows in Pollard's tables.
- Use the Pollard event sequence.

After an administrator creates the schema objects, an application role can be
limited to row access and sequence use. PostgreSQL network controls, TLS,
credential rotation, backups, and row-level tenant policy remain operator
responsibilities.

`store_id` separates logical Pollard stores inside one database. Workers share
governance only when both the DSN target and `store_id` match. Store ids are not
an authorization boundary; database permissions provide that boundary.

## Concurrent Node Writes

Node identity remains content-addressed. Two workers inserting the same node
race through `INSERT ... ON CONFLICT DO NOTHING`; the stored identity is the
same either way. Different identity fields under one id remain an integrity
error. Metadata updates take a row lock before merging their top-level patch.

PostgreSQL and SQLite both intern large string leaves while preserving the
canonical rehydrated payload. Interned blobs remain plaintext and follow the
same data classification as the original payload.

## Shared Budget Reservations

On a transactional store, each governed model or tool call uses three steps:

1. Precheck atomically reserves each known estimate against every active budget
   scope.
2. The caller function executes only after that transaction succeeds.
3. Settlement removes the reservation and adds the actual charge.

Reservations have a lease. A process that exits after precheck cannot hold
capacity forever; a later precheck removes expired reservations. Set
`reservation_lease_seconds` longer than the expected call duration:

```python
runtime = Runtime(store, reservation_lease_seconds=180)
```

With concurrent writers sharing one arbiter, total settled spend is bounded by
the budget plus the sum of actual-minus-estimate overshoot for calls that passed
precheck. Meters with exact prechecks, including steps and request windows, do
not exceed the limit. An approximate token estimate can still settle above its
reservation because the provider spend has already occurred.

The arbiter tables are mutable coordination state. They are not part of node
identity or subtree seals. A backup used for live continuation should include
both the node and arbiter tables.

## Sliding Windows

`WindowMeter(name, limit, window_seconds)` stores settled events and active
reservations in the selected store. The default `requests` behavior counts one
model or tool call and is exact:

```python
from pollard import Runtime, WindowMeter
from pollard.meters import StepMeter

runtime = Runtime(
    store,
    meters=[StepMeter(), WindowMeter("requests", 120, 60)],
)
```

For tokens, wrap an estimating `TokenMeter` so precheck can reserve input and
output capacity:

```python
from pollard import Runtime, WindowMeter
from pollard.estimators.openai import OpenAITokenEstimator
from pollard.meters import StepMeter, TokenMeter

token_meter = TokenMeter(
    OpenAITokenEstimator(),
    reserved_output_tokens=2_048,
)
runtime = Runtime(
    store,
    meters=[
        StepMeter(),
        WindowMeter("tokens", 100_000, 60, meter=token_meter),
    ],
)
```

Window scope is the run root plus the meter configuration. Resuming the same
run preserves the window, and workers on the same root see each other's events.
A window refusal uses `reason="window"` and records `window_seconds` in its
identity payload.

## Merge Disconnected Stores

`merge(destination, source)` copies missing nodes, validates equal-id identity,
keeps the destination result on result collisions, and records the incoming
result under mutable metadata. `merge(..., replay=True)` raises an integrity
error instead of accepting a result collision.

Metadata union never removes a key. Nested objects are combined, lists are
unioned by canonical JSON value, and conflicting scalar values are recorded
under `meta["merge_conflicts"]`. Repeating a merge does not add duplicate
conflict records.

```python
from pollard import SQLiteStore, merge

with SQLiteStore("combined.db") as destination:
    with SQLiteStore("worker.db") as source:
        report = merge(destination, source)
print(report.to_dict())
```

Use replay mode when importing a recording into a deterministic replay corpus.
Use the default keep-first behavior when joining audit ledgers where observing
the conflict is more useful than rejecting the union.

## Multi-Store CLI

`runs` accepts one or more store specifications, and `merge` accepts one
destination followed by one or more sources:

```powershell
pollard runs worker-a.db worker-b.db --json
pollard merge combined.db worker-a.db worker-b.db --json
```

A PostgreSQL URI may use its fragment as the logical store id, but placing a
password on the command line can expose it to process inspection. The preferred
form reads the DSN from an environment variable:

```powershell
$env:POLLARD_PG_DSN = "postgresql://pollard_app:password@db.example/pollard"
pollard runs "pg-env:POLLARD_PG_DSN#support-prod" --json
pollard merge combined.db "pg-env:POLLARD_PG_DSN#support-prod" --json
```

CLI output labels the environment variable and store id, never the DSN value.
Plain SQLite paths remain compatible with earlier releases.

## Operations Boundary

- Use PostgreSQL for several hosts or sustained writer contention.
- Keep call duration below the configured lease, or increase the lease.
- Run garbage collection only during a coordinated offline maintenance window.
- Monitor database availability and capacity independently of Pollard.
- Do not claim fail-closed coordination between disconnected stores. Merge is
  an audit-ledger union after the fact, not a distributed budget protocol.
