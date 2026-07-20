# PostgreSQL Operations

This guide covers schema upgrades, lease behavior, connection recovery,
backup, restore, and external seal custody for `PostgresStore`. Perform schema
changes in a maintenance window with provider traffic stopped.

Pollard CI exercises PostgreSQL 14, 15, 16, 17, and 18, the major release lines
listed as supported by the upstream
[PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/)
when Pollard 1.0.3 was prepared. Use the current minor release within the
selected major line.

## Schema contract

Pollard 1.0.3 uses PostgreSQL schema version 2. A new, empty target is created
at version 2 on first connection. An existing unversioned Pollard database is
recognized as the legacy schema from Pollard 1.0.2 and earlier, but it is not
changed during normal construction. `PostgresStore` refuses it with a migration
required error. A version newer than the installed package is always refused.

Read the version without opening application traffic:

```sql
SELECT version, updated_at
FROM pollard_schema
WHERE singleton = 1;
```

The legacy schema has no `pollard_schema` table. Do not add that table by hand.

## Backup and forward migration

Use an administrator connection for backup and migration. Put connection
settings in a libpq service entry so a password is not exposed in a command
argument. The PostgreSQL documentation describes
[`pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html),
[`pg_restore`](https://www.postgresql.org/docs/current/app-pgrestore.html), and
[libpq service files](https://www.postgresql.org/docs/current/libpq-pgservice.html).

1. Stop every Pollard worker that uses the database.
2. Confirm that no reservation remains in flight:

```sql
SELECT store_id, reservation_id, expires_at
FROM pollard_reservations
ORDER BY store_id, reservation_id;
```

3. Create a complete custom-format backup and a checksum:

```powershell
pg_dump --dbname="service=pollard-prod" --format=custom --no-owner `
  --file=pollard-before-1.0.3.dump
Get-FileHash pollard-before-1.0.3.dump -Algorithm SHA256
```

4. Save the dump and checksum under the deployment's backup retention policy.
5. Run the explicit migration with the same administrative target:

```powershell
$env:POLLARD_PG_DSN = "service=pollard-prod"
python -c "import os; from pollard import PostgresStore; print(PostgresStore.migrate(os.environ['POLLARD_PG_DSN']))"
```

The expected legacy result is `(0, 2)`. Migration refuses a partial legacy
schema, an unknown version, or any nonempty reservation table. Repeating it on
version 2 returns `(2, 2)` without changing data.

6. Open one `PostgresStore`, run a read and write canary under a test
   `store_id`, then resume workers.

## Restore drills

Restore into a new, empty database. Do not overwrite the only production copy.

```powershell
createdb --maintenance-db="service=pollard-admin" pollard_restore_test
pg_restore --dbname="service=pollard-restore-test" --exit-on-error `
  --single-transaction pollard-before-1.0.3.dump
```

For audit-only recovery, a sealed subtree export is enough to verify and replay
the recorded nodes. It does not restore shared budget state, reservations, or
sliding-window events.

For live continuation, restore the complete Pollard schema. It includes nodes,
interned blobs, budget state, reservation state, active reservation rows,
window scopes, window events, and the window event sequence. A planned backup
should be taken after workers drain, so the restored reservation table is
empty. If an emergency backup contains an in-flight reservation, do not resume
traffic until the corresponding provider outcome and settlement state have
been reconciled.

Before directing traffic to the restored database:

- confirm schema version 2;
- run `pollard verify` or the Python `verify()` API for each required root;
- compare subtree digests with the external custody log;
- confirm the intended budget and window state;
- run strict replay with callables that fail if execution occurs; and
- open a new `PostgresStore` rather than reusing a connection to the old server.

## Reservation leases

Transactional model and tool calls renew their reservation while the caller
function is running. The default lease is 60 seconds. Renewal uses a separate
short PostgreSQL connection, so the main store connection remains available to
the calling code.

Set the lease longer than expected database interruptions and configure the
libpq connection timeout below the lease. A healthy call can run longer than
one initial lease because renewal extends the expiry. If renewal cannot be
confirmed before expiry, Pollard still attempts to settle and record the
completed call, then raises `ReservationLeaseLost` with `reservation_id` and
`node_id`. Do not describe that call as protected by an exact shared limit.

## Lost connections and retries

Reserve and settle transactions are keyed by a random reservation ID. Repeating
the same reserve request returns the existing active reservation. Repeating a
settlement with the same charges is a no-op. Repeating it with different
charges is an integrity error.

`PostgresStore` reconnects once after a connection error during reserve,
settle, or release, then repeats the idempotent operation. This covers a commit
whose acknowledgement was lost. If the second outcome is still unknown:

- `ReservationUncertain` means the provider callable was not started by that
  reserve attempt. Keep the reservation ID for incident review and wait for
  expiry before assuming the capacity is free.
- `SettlementUncertain` means the provider callable completed, but Pollard
  cannot confirm its database charge. Do not repeat the provider call. Reopen
  the database, inspect `pollard_reservation_state`, and retry settlement with
  the same reservation ID and exact charges.

After a database restart, create a new `PostgresStore` or call `reconnect()` on
each long-lived instance before ordinary reads and writes. Reserve, settle, and
release already perform one reconnect attempt themselves.

## External seal custody

The reference sink appends seal publications to a SQLite file that must be
separate from the Pollard store:

```python
from pollard import SQLiteSealSink, seal

report = seal(store, root_id)
sink = SQLiteSealSink("/independent-control/pollard-seals.db")
record = sink.publish(
    report,
    store_id=store.store_id,
    signer_identity="deployment:pollard-prod",
)
print(record.to_dict())
```

Each row contains a monotonic sequence, store ID, root ID, seal algorithm,
digest, UTC time, and signer identity. `signer_identity` is an operator label,
not a digital signature. Place the sink under different credentials and backup
control from the Pollard database. A production deployment can forward the
same fields to object lock, a transparency log, or its existing signing system.
Key custody, signature verification, and timestamp authority remain deployment
responsibilities.
