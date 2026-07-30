# Store Backend Validation

This release is package engineering. It does not change or reinterpret the
submitted paper repository or its evidence.

## Classification

| Finding | Classification | Release action |
|---|---|---|
| Redis transactional storage | Production feature and hardening | Added exact optimistic transactions and documented failover limits |
| Redis URL-only client construction | Pollard hardening opportunity | Added a caller-owned fresh-client factory and forced Sentinel failover acceptance |
| CLI merge retained every prepared source tree simultaneously in memory | Scale and fail-closed preparation limitation | Replaced aggregate plans with deterministic per-source private SQLite spools that are finalized and validated before destination environment lookup or access |
| Redis CLI merge destination | CLI capability and safety boundary | Added existing-only environment-backed destinations with explicit selectors, plus opt-in atomic initialization after complete source materialization and idempotent retry |
| MongoDB transactional storage | Production feature and hardening | Added replica-set transactions and standalone refusal |
| MongoDB CLI merge destination | CLI capability and safety boundary | Added explicit environment-backed destinations, existing-only zero-write validation, fail-closed partial/index state checks, and opt-in logical initialization after source materialization |
| MongoDB reconnect discarded the prior client before replacement validation | Pollard hardening defect | Replacement now passes topology, index, and schema checks before the previous client closes |
| Neo4j transactional storage | Production feature and hardening | Added explicit coordinator locking to prevent lost updates |
| Neo4j CLI merge destination | CLI capability and safety boundary | Added explicit environment-backed destinations, existing-only zero-write validation of logical, global constraint, and owned online range-index state, and opt-in fresh logical initialization after source materialization |
| Neo4j empty-schema reads emitted missing-property notifications | Pollard production-hardening defect | Read the property map atomically and retain fail-closed field validation without server warning noise |
| Neo4j cluster failover had not been exercised | Validation gap, not a Pollard defect | Accepted forced writer loss on a local three-primary Enterprise evaluation cluster |
| Kafka lacks arbitrary state compare-and-swap | External system behavior | Added Store-only event log; no shared-arbiter claim |
| Kafka client configuration is a mapping rather than one URI | CLI design boundary | Added a strict environment-backed JSON mapping, read-only producer-free construction, and frozen high-watermark observation |
| Kafka CLI merge destination | CLI capability with an immutable identity boundary | Added explicit environment-backed, existing-and-populated destinations that prove store identity through full replay before producer construction; empty or missing topics remain operator/Python initialization concerns |
| Configured example claimed repeated recording was idempotent | Example and documentation defect | Preserve the settled budget and refuse reuse with an actionable fresh-label instruction |
| Existing PostgreSQL G2 behavior | Regression risk | Re-ran schema, lease, reconnect, duplicate, ambiguity, and custody tests |
| Remote-store operating detail was concentrated in one overview | Documentation hardening | Added four backend-specific production, security, monitoring, failover, rotation, and recovery guides |

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
- Remote CLI acceptance: environment-backed Redis, MongoDB, Neo4j, and
  Kafka sources for inspection, export, runs, and merge input; existing-only or
  read-only construction; and credential-safe labels. Shared CLI merge
  acceptance covers multiple large sources with one disk-backed spool per
  source and bounded aggregate preparation state; exact node fidelity and
  deterministic source/node replay; result and metadata conflict behavior;
  source open, traversal, validation, serialization, disk-write, and
  finalization failure before destination environment lookup or access; corrupt
  and truncated spool refusal; cleanup attempted after success, source failure,
  destination-constructor failure, and destination-body failure; cleanup
  failure as a credential-safe command error that can leave private artifacts;
  primary attribution plus a fixed cleanup notice on combined failure;
  exact-rerun idempotence; JSON per-source reports; and Windows-safe close,
  reopen, rename, and removal.
  Destination selector syntax and flag eligibility may be checked before
  preparation, but selector validation neither reads referenced destination
  environment values nor constructs a client. Redis destination
  acceptance covers required explicit prefix and store id, environment-only
  existing namespace validation with `create=False`, complete disk-backed
  source preparation before destination access, flag-scoped `create=True` atomic
  initialization, exact-rerun idempotence, partial-copy behavior, malformed
  selector refusal, text-decoding-option refusal, and direct-URL refusal.
  MongoDB destination acceptance covers explicit database, prefix, and store
  id, environment-only construction, zero artifacts for missing existing-only
  destinations and failed source preflight, fresh transactional logical
  initialization, exact unique-index validation, partial-state refusal,
  credential-safe driver failures, `create=False` reopen, and an unchanged
  exact rerun. Live destination tests are conditional on
  `POLLARD_TEST_MONGODB_URI`. Neo4j destination acceptance covers explicit
  URI, user, and password environment references, database, and store id;
  direct-URI refusal; zero-write existing-only validation of schema,
  coordinator, exact global constraints, and their owned online range indexes;
  source preflight before
  destination access; fail-closed partial logical and constraint/index states;
  fresh
  logical initialization; credential-safe failures; reconnect; exact-rerun
  idempotence; and node-by-node partial-copy behavior. Live destination tests
  are conditional on `POLLARD_TEST_NEO4J_URI`.
  Kafka destination acceptance covers an explicit configuration reference,
  topic, and store id; direct-URI and initialization-flag refusal; complete
  finalized and validated source spools before destination configuration lookup
  or access;
  missing, empty, wrong-identity, corrupt, truncated, and incompatible topic
  refusal before producer construction; producer creation only after full
  retained-history and prefix validation; credential-safe constructor and
  delivery failures; result and metadata conflicts; append-by-append partial
  failure; and exact rerun without new events. Live destination acceptance is
  conditional on `POLLARD_TEST_KAFKA_BOOTSTRAP` and cleans up only the exact
  unique test topic.
- Failure acceptance: missing or future schema, corrupt records, incompatible
  topology, malformed Kafka events, truncated Kafka history, and closed-client
  behavior.
- Compatibility acceptance: supported Python versions, PostgreSQL 14 through
  18, real Redis, MongoDB replica-set, Neo4j Community, and Apache Kafka
  containers, plus source and wheel installation tests. The 1.1.1 pass adds
  Redis Sentinel failover, a three-member MongoDB replica set, a three-primary
  Neo4j Enterprise evaluation cluster, and a three-broker Kafka minimum-ISR
  topology.
- Coverage acceptance: total package line coverage must remain above 90
  percent with the remote-service suite enabled.

## Observed Release Results

- Python 3.12 local release suite: 933 passed, 52 skipped, and 9 warnings.
  Forty-nine skips are conditional PostgreSQL, Redis, MongoDB, Neo4j, and Kafka
  checks whose services were not configured; three are optional-framework
  recipe checks for unavailable required LangChain or pydantic-ai versions.
  Ruff passed, strict mypy passed across 45 source files, and the focused
  documentation, README-link, evidence, and import set passed all 17 tests.
  The 1.4.0 sdist and wheel built successfully; wheel metadata and an isolated
  install both reported 1.4.0. Version-consistency and stale-language searches,
  secret/generated-artifact review, and `git diff --check` also passed.
- Python 3.12 full suite with PostgreSQL 18, Redis 8.0, MongoDB 8.0 replica
  set, Neo4j 5.26 Community, and Apache Kafka 4.3.1: 491 passed, one
  broker-depth test intentionally skipped, and more than 90.5 percent package
  line coverage from a fresh coverage database.
- Python 3.10 and 3.14 storage-critical suites passed on each interpreter with
  the same explicit Kafka depth skip.
- PostgreSQL 14, 15, 16, and 17 acceptance: 97 passed on each version.
  PostgreSQL 18 ran in the full all-backend suite.
- Forced restart acceptance: persisted nodes survived service restart and
  same-object `reconnect()` for Redis, MongoDB, Neo4j, and Kafka. Transactional
  stores also retained settlement tombstones and rejected changed duplicate
  charges after restart.
- Redis Sentinel acceptance used three Sentinel processes, one primary, and
  one synchronized replica. After forced primary loss, the caller-owned
  factory followed the promoted replica, same-object reconnect retained the
  node, and changed settlement retry remained an integrity error.
- MongoDB failover acceptance used three replica-set members. After forced
  primary loss, a new primary was elected, atomic reconnect retained the node,
  and duplicate-settlement validation remained fail closed.
- Neo4j failover acceptance used three 5.26.28 Enterprise primary allocations.
  After the exact writer was killed, a routed write completed through a
  surviving seed, a replacement writer was elected, same-object reconnect
  retained the run, duplicate settlement remained idempotent, changed charges
  failed closed, and strict replay, verification, and independent
  `SQLiteSealSink` custody passed.
- Kafka failover acceptance used three brokers, replication factor 3, and
  `min.insync.replicas=2`. One-broker loss retained writes and replay; a
  second loss refused the write; recovery plus deterministic retry produced
  one valid tree state.
- The configured walkthrough completed record, strict replay, verification,
  and sealing against all five remote backends. A repeated transactional run
  label was refused before execution with a fresh-label instruction, and an
  empty Neo4j store initialized without missing-property notifications.
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
- MongoDB needs a replica set or sharded deployment. The same-host
  three-member acceptance validates election and reconnect, not independent
  failure domains or arbitrary network partitions.
- Neo4j serializes each logical store through one coordinator node. This favors
  exact accounting over maximum write throughput.
- Kafka has no shared budget arbitration or record-level GC. Cold replay and
  memory grow with the retained log and materialized tree. A read-only instance
  freezes one committed prefix and sees later appends only after reconnect.
  Because `store_id` is carried by events rather than a separate schema record,
  an empty topic cannot prove which store id was intended.
- The command-line store selector can open ordinary URL-backed Redis and
  environment-backed MongoDB, basic-auth Neo4j, or environment-configured Kafka
  as observational/read or merge sources. `redis-env:`, `mongo-env:`, and
  `neo4j-env:` are remote merge destinations; `kafka-env:` is a narrower
  existing-and-populated merge destination. Kafka still lacks shared
  arbitration and record-level GC. Ordinary destinations use existing-only
  construction. Redis requires explicit prefix and store id; MongoDB requires
  explicit database, prefix, and store id; Neo4j requires explicit URI, user,
  and password environment references, database, and store id; Kafka requires
  an explicit configuration reference, topic, and store id.
  `--initialize-if-missing` opts into fresh Redis, MongoDB, or Neo4j
  initialization only after every source has been materialized. It remains
  invalid for Kafka because Pollard never creates topics or seeds their first
  identity-proving node. Redis initialization is one transaction.
  MongoDB index DDL is separate from its atomic schema/coordinator transaction
  and can leave an empty index-only shell after later failure. Neo4j
  database-global constraint DDL is separate from its atomic
  schema/coordinator transaction and may leave shared constraints after later
  failure. Ambiguous or partial logical states are not repaired. A Kafka
  destination must fully replay at least one node proving its identity before
  producer construction; missing, empty, wrong-identity, corrupt, truncated,
  or incompatible topics publish nothing. Direct Redis, MongoDB, Neo4j, Bolt,
  and Kafka URLs are rejected as destinations. Sentinel or
  `client_factory` deployments, advanced PyMongo options, Neo4j authentication
  managers and advanced driver configuration, and callback-based or other
  non-JSON Kafka configuration remain Python API concerns so callers retain
  ownership of credential and client policy. Redis Cluster remains outside the
  supported release matrix. Kafka sources use a producer-free frozen read
  view.
- Redis, MongoDB, Neo4j, and Kafka CLI merge application is a sequence of
  per-node writes; Kafka realizes those writes as append-only events. It is not
  a cross-node or cross-backend transaction. An interrupted merge can retain
  accepted changes; an exact rerun is idempotent. Concurrent destination
  writers can race merge metadata updates, so quiesce writers, verify, and
  seal. Active reservations, settled counters, rate-window events, and leases
  are not copied. CLI preparation uses private per-source disk spools and
  bounded working memory, so temporary-disk capacity and access control remain
  operator responsibilities. Cleanup is attempted on every exit, but failure
  can leave private artifacts, fails an otherwise successful command, preserves
  primary attribution on combined failure, and does not roll back accepted
  destination writes. Future improvements may add a transactional
  per-node merge primitive or stronger explicit quiescence enforcement. Kafka
  events accepted before failure are irreversible through Pollard; its exact
  rerun publishes no new events.
- A new Neo4j driver, including one created by `reconnect()`, still needs a
  reachable router from the configured URI or caller-owned resolver. Cluster
  routing cannot recover a client whose only seed is down before discovery.
  Neo4j CLI reads remain write-routed to a primary and use read-committed
  isolation rather than a command-wide snapshot. Neo4j's single coordinator
  remains a logical-store write hot spot; arbitrary router-discovery and
  topology failures remain outside local acceptance.
- The local multi-node Redis, MongoDB, Neo4j, and Kafka checks force process
  loss on one host. They do not establish safety for every managed service,
  Region, storage failure, or network partition. MongoDB arbitrary partitions
  and independent failure domains, Neo4j arbitrary partitions and router
  discovery, and remote deployment durability guarantees remain outside local
  acceptance.
- Pollard does not coordinate limits across disconnected databases or across
  different logical store ids.
