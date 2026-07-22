# Store Backend Validation

This release is package engineering. It does not change or reinterpret the
submitted paper repository or its evidence.

## Classification

| Finding | Classification | Release action |
|---|---|---|
| Redis transactional storage | Production feature and hardening | Added exact optimistic transactions and documented failover limits |
| MongoDB transactional storage | Production feature and hardening | Added replica-set transactions and standalone refusal |
| Neo4j transactional storage | Production feature and hardening | Added explicit coordinator locking to prevent lost updates |
| Kafka lacks arbitrary state compare-and-swap | External system behavior | Added Store-only event log; no shared-arbiter claim |
| Existing PostgreSQL G2 behavior | Regression risk | Re-ran schema, lease, reconnect, duplicate, ambiguity, and custody tests |

No experiment-runner defect or model-provider behavior is involved in these
storage additions.

## Submitted Experiment Incident Audit

The submitted E5 evidence was inspected read-only. Its failure classes do not
all belong in Pollard core:

| E5 finding | Classification | Package status |
|---|---|---|
| Strict-tool limits and provider schema dialects | Experiment integration plus provider constraint | Local JSON Schema references and object closure were hardened in 1.0.6; request-specific tool selection remains caller-owned |
| Unresolved `$ref` and implicit object closure | Pollard correctness defect | Fixed in 1.0.6 with deterministic expansion and fail-closed validation |
| Token-count and generation request projection differ | Adapter correctness defect | Fixed in 1.0.6 by provider-specific count projection; generation-only fields are excluded explicitly |
| Provider returns parallel tool calls to a single-call runner | External provider behavior plus experiment policy | Pollard preserves calls; the caller decides whether to execute, serialize, or reject them |
| Model accepts metadata or token count but is unavailable for generation | External provider behavior | A generation attempt remains uncertain and is conservatively settled; Pollard does not cache availability claims |
| Provider errors lose native detail before recording | Pollard hardening plus experiment integration | Direct adapters preserve native errors and structured terminal details in 1.0.6 and 1.0.7; custom clients must retain their own raw failures |
| Foreground interruption leaves a child process alive | Experiment supervision defect | Pollard handles interruption at its call boundary but does not own or terminate caller processes |
| Ambiguous settlement, renewal, and retry auditability | Pollard correctness and hardening | Fixed through 1.0.5 to 1.0.7 and rechecked against every transactional backend in 1.1.0 |

No submitted result, manifest, prompt, transcript, or paper source was changed
as part of this audit.

## Frozen Test Matrix

- Shared Store acceptance: idempotent put, parent enforcement, deterministic
  children, roots and deep walk, metadata merge, and result conflict handling.
- Transactional acceptance: exact concurrent budgets and request windows,
  duplicate reserve and settle, changed retry rejection, release, expiry,
  renewal, reconnect, server-time settlement, and explicit uncertainty.
- Evidence acceptance: record and strict replay, `verify()`, `seal()`, merge,
  governance, and independently stored `SQLiteSealSink` publications.
- Failure acceptance: missing or future schema, corrupt records, incompatible
  topology, malformed Kafka events, truncated Kafka history, and closed-client
  behavior.
- Compatibility acceptance: supported Python versions, PostgreSQL 14 through
  18, real Redis, MongoDB replica-set, Neo4j Community, and Apache Kafka
  containers, plus source and wheel installation tests.
- Coverage acceptance: total package line coverage must remain above 90
  percent with the remote-service suite enabled.

## Observed Release Results

- Python 3.12 full suite with PostgreSQL 18, Redis 8.0, MongoDB 8.0 replica
  set, Neo4j 5.26 Community, and Apache Kafka 4.3.1: 468 passed, one
  broker-depth test intentionally skipped, and 90.11 percent package line
  coverage from a fresh coverage database.
- Python 3.10 and 3.14 storage suites: 153 passed on each interpreter, with
  the same explicit Kafka depth skip.
- PostgreSQL 14, 15, 16, and 17 acceptance: 97 passed on each version.
  PostgreSQL 18 ran in the full all-backend suite.
- Forced restart acceptance: persisted nodes survived service restart and
  same-object `reconnect()` for Redis, MongoDB, Neo4j, and Kafka. Transactional
  stores also retained settlement tombstones and rejected changed duplicate
  charges after restart.
- Release artifacts: Twine accepted both archives; the wheel plus `stores`
  extra and the source archive each passed an isolated install and import
  canary outside the source checkout.

## Cloud Spend

No model inference is relevant to database transaction semantics. The separate
authorized cloud ledger was not opened, no paid provider request was made, and
spend is `0.00 USD` of the `8.00 USD` ceiling. No local GPU was used.

## Remaining Limits

- Redis durability depends on persistence, replication, and failover policy;
  asynchronous failover is not PostgreSQL-equivalent durability.
- MongoDB needs a replica set or sharded deployment. One-member test topology
  does not represent production fault tolerance.
- Neo4j serializes each logical store through one coordinator node. This favors
  exact accounting over maximum write throughput.
- Kafka has no shared budget arbitration or record-level GC. Cold replay and
  memory grow with the retained log and materialized tree.
- The command-line store selector remains PostgreSQL-focused. New remote
  backends are configured through the Python API so callers retain ownership
  of credential and client-policy construction.
- The local service matrix uses one-node deployments. It validates adapter
  recovery and persistence, not multi-node failover safety under partition.
- Pollard does not coordinate limits across disconnected databases or across
  different logical store ids.
