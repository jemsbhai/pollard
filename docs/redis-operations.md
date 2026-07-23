# Redis Store Operations

This guide is the production contract for `RedisStore`. Use the
[distributed-store guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
to compare it with PostgreSQL, MongoDB, Neo4j, and Kafka.

## Capability And Boundary

RedisStore is a synchronous Store, TransactionalArbiter, and RenewableArbiter.
It serializes one logical store with WATCH, MULTI, and EXEC on a revision key.
All keys for one `store_id` share a hash tag, and accounting values remain
exact decimal strings.

Redis acknowledgement is not the same durability guarantee as a PostgreSQL
commit. Redis replication is asynchronous and a promoted replica can lack an
acknowledged write. Pollard detects missing identity or schema state after
reconnect, but it cannot reconstruct data the Redis deployment lost.

## Install And Direct Connection

```powershell
python -m pip install "pollard[redis]"
$env:POLLARD_REDIS_URL = "rediss://pollard_app:password@redis.example:6379/0"
```

```python
import os
from pollard import RedisStore

store = RedisStore(
    os.environ["POLLARD_REDIS_URL"],
    store_id="support-prod",
    prefix="pollard",
    watch_retries=64,
)
```

The URL path selects the Redis database for a non-clustered deployment.
`watch_retries` limits only optimistic transaction conflicts. Network retry,
connect timeout, socket timeout, health checks, and TLS policy belong to the
redis-py client configuration.

## Sentinel And Caller-Owned Client Construction

Use `client_factory` when the application owns Sentinel discovery or a
managed-primary client. The factory must return a fresh synchronous redis-py
client on every call and must enable `decode_responses=True`. Pollard invokes
it during construction and `reconnect()`; it does not retain Sentinel
credentials or choose a failover policy.

```python
import os
from redis.sentinel import Sentinel
from pollard import RedisStore

sentinel = Sentinel(
    [("sentinel-a.example", 26379), ("sentinel-b.example", 26379)],
    sentinel_kwargs={
        "username": os.environ["REDIS_SENTINEL_USER"],
        "password": os.environ["REDIS_SENTINEL_PASSWORD"],
        "ssl": True,
    },
    username=os.environ["REDIS_DATA_USER"],
    password=os.environ["REDIS_DATA_PASSWORD"],
    ssl=True,
    socket_connect_timeout=2,
    socket_timeout=5,
)

def redis_primary():
    return sentinel.master_for(
        "pollard-primary",
        decode_responses=True,
        check_connection=True,
    )

store = RedisStore(
    client_factory=redis_primary,
    store_id="support-prod",
)
```

Sentinel itself should have at least three independently placed members and a
quorum appropriate to the deployment. Test that the factory follows a promoted
primary and that a changed primary cannot serve stale Pollard state.

Redis Cluster is not in Pollard's supported release matrix. Although Pollard
keys share one cluster slot and redis-py supports same-slot transaction
pipelines, Pollard also requires a server-time read within its transaction
contract. Do not pass `RedisCluster` through the factory until that complete
path has passed a deployment-specific acceptance test.

## Required Redis Policy

- Configure `maxmemory-policy noeviction`. Any eviction can remove nodes,
  reservations, retry tombstones, or schema identity.
- Enable AOF or another persistence mode with a recovery point that matches the
  application's accepted data-loss window.
- Use replicas and backups on separate failure domains. Replication alone is
  not a backup.
- Restrict destructive administration such as FLUSHDB, FLUSHALL, DEL on the
  Pollard prefix, RESTORE, MIGRATE, and unsafe configuration changes.
- Keep all workers on the same Redis deployment, database, prefix, and
  `store_id`.

The application identity needs PING, TIME, WATCH, MULTI, EXEC, GET, INCR, HGET,
HGETALL, HSET, and HDEL for Pollard keys. redis-py can also need transaction
cleanup commands such as UNWATCH or DISCARD. Sentinel discovery credentials are
separate from data-node credentials.

## Timeouts, Retries, And Reconnect

Set finite `socket_connect_timeout` and `socket_timeout` values that fit
inside the application's call deadline and reservation lease. Account for DNS,
TLS handshakes, Sentinel election time, and worker scheduling stalls.

Do not add an unbounded transport retry loop around a Pollard operation.
Pollard retries exact reservation, settlement, and release identities once
after reconnect. A second connection failure becomes
`ReservationUncertain` or `SettlementUncertain`. Charge an ambiguously
dispatched provider call at its reserved ceiling until reconciliation.

`reconnect()` creates a fresh client, pings it, and verifies the existing
store identity, schema version, and revision before replacing the prior
client. A factory that returns the same object is refused.

## Monitoring

Alert on:

- evicted keys greater than zero;
- AOF or snapshot persistence errors and delayed fsync;
- primary changes, replica lag, and insufficient healthy replicas;
- Sentinel quorum failure or master discovery failure;
- Redis command timeout, READONLY, MASTERDOWN, CLUSTERDOWN, and BUSYLOADING
  errors;
- repeated WATCH conflicts near `watch_retries`;
- reservation uncertainty, lease loss, and schema or revision integrity
  errors; and
- memory growth for the Pollard prefix.

Do not place prompts, results, credentials, or Redis URLs in metrics or logs.
Store exception type, logical `store_id`, reservation id, and a redacted
endpoint label.

## Backup, Restore, And Credential Rotation

For a planned backup, stop new calls and drain active reservations first.
Capture the Redis persistence artifact with its configuration and checksum.
Restore into a separate target, never over the active primary.

Before cutover:

1. Open a new RedisStore against the restored target.
2. Confirm construction accepts the identity and schema.
3. Run `verify()` for required roots.
4. Compare `seal()` with a seal held under different credentials.
5. Strictly replay a representative run with callables that fail if executed.
6. Inspect settled budgets, active reservations, and window state.
7. Move traffic only after the recovery point is accepted.

Rotate credentials by creating and testing a new ACL identity first, replacing
the caller-owned URL or factory configuration, calling `reconnect()`, and
then revoking the old identity. Do not change `store_id` during rotation.

## Production Acceptance

Run these checks on a non-production logical store before enablement:

- two clients contend for an exact step budget and never exceed it;
- a process restart preserves nodes and duplicate-settlement tombstones;
- primary failover is forced while reservations and reads are active;
- the application stops on uncertain outcomes and reconciles the same ids;
- credential rotation succeeds without changing the logical store;
- a backup restores to an isolated target;
- strict replay, `verify()`, and an external seal comparison pass; and
- persistence and replica-loss behavior match the declared recovery objective.

Official references: [redis-py connections](https://redis.readthedocs.io/en/stable/connections.html),
[Redis transactions](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/),
[Redis Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/),
and [WAIT durability limits](https://redis.io/docs/latest/commands/wait/).
