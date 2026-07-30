# Kafka Store Operations

This guide is the production contract for `KafkaStore`. KafkaStore is an
ordered audit and replay backend, not a shared budget arbiter.

## Capability And Boundary

One dedicated, single-partition topic is one logical Pollard store. KafkaStore
appends canonical `put` and metadata commands, assigns a direct read-committed
consumer at offset zero, and materializes the complete log in memory.

Kafka transactions do not compare arbitrary shared budget state for Pollard.
KafkaStore therefore has no reservation, renewal, shared-window, or physical
garbage-collection capability. Several Runtime instances using the same topic
do not share an exact budget.

Cold start is linear in retained events. Memory grows with nodes and operation
outcomes. If bounded replay time, finite retention, selective erasure, or
shared arbitration is required, choose another backend.

## Install And Provision

```powershell
python -m pip install "pollard[kafka]"
```

Create the topic explicitly with a production replication factor and durability
policy:

```powershell
kafka-topics.sh `
  --bootstrap-server broker-1.example:9093 `
  --create `
  --topic pollard-support-prod `
  --partitions 1 `
  --replication-factor 3 `
  --config cleanup.policy=delete `
  --config retention.ms=-1 `
  --config retention.bytes=-1 `
  --config min.insync.replicas=2
```

Keep unclean leader election disabled for this topic or cluster. With
`acks=all`, replication factor 3, and `min.insync.replicas=2`, a write is
refused when too few in-sync replicas remain instead of being acknowledged by a
single replica. This is an operator durability policy, not a Pollard-enforced
topic check.

Pollard validates one partition, delete-only cleanup, infinite time and byte
retention, and log start offset zero. It does not validate rack placement,
replication factor, minimum ISR, unclean election, broker disk policy, or
cross-region recovery.

## Connect With Caller-Owned Security

```python
import os
from pollard import KafkaStore

store = KafkaStore(
    {
        "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "SCRAM-SHA-512",
        "sasl.username": os.environ["KAFKA_USERNAME"],
        "sasl.password": os.environ["KAFKA_PASSWORD"],
        "ssl.ca.location": os.environ["KAFKA_CA_FILE"],
        "client.id": "pollard-support",
    },
    topic="pollard-support-prod",
    store_id="support-prod",
    timeout=30,
)
```

The configuration mapping remains caller-owned. Pollard removes and replaces
settings that affect its ordering contract: acknowledgements, idempotence,
consumer group and offset behavior, partition EOF, earliest reset, and
read-committed isolation. `transactional.id` is refused because Pollard uses
deterministic application operation ids.

The writer principal needs topic metadata and configuration description, READ
and WRITE for the dedicated topic, and the cluster permission required for an
idempotent producer. Grant no ALTER, DELETE, or CREATE permission to the normal
application identity. Its directly assigned consumer may also discover the
group coordinator, so grant GROUP DESCRIBE for the deterministic
`pollard-reader-<sha256(topic + NUL + store_id)>` identity. Pollard does not
join that group or commit offsets. Use a separate administrator for
provisioning.

## CLI Inspection And Merge

The CLI can inspect or merge into an existing topic without putting brokers or
credentials in process arguments. Store the confluent-kafka mapping as JSON in
an environment variable and select the dedicated topic in the store
specification:

```powershell
$env:POLLARD_KAFKA_CONFIG = '{"bootstrap.servers":"broker-1.example:9093,broker-2.example:9093","security.protocol":"SASL_SSL","sasl.mechanism":"SCRAM-SHA-512","sasl.username":"pollard_reader","sasl.password":"<secret>","ssl.ca.location":"C:\\certs\\kafka-ca.pem"}'
$kafkaStore = "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard-support-prod&timeout=120#support-prod"

pollard runs $kafkaStore --json
pollard show $kafkaStore <root-id>
pollard report $kafkaStore <root-id> --json
pollard verify $kafkaStore --json
pollard seal $kafkaStore <root-id> --output seal.json --json
pollard export $kafkaStore <root-id> subtree.json --json
pollard merge combined.db $kafkaStore --json
pollard merge $kafkaStore runs.db --json
```

The grammar is
`kafka-env:CONFIG_VAR?topic=TOPIC&timeout=SECONDS#store-id`. `topic` is required
exactly once. `timeout` is an optional positive integer and defaults to `30`;
store id defaults to `default` for sources. A destination must explicitly
provide the configuration-variable reference, topic, and a nonempty
`#store-id` fragment. Timeout is an operational replay bound rather than store
identity, so the safe CLI label contains the configuration-variable name,
topic, and store id but omits timeout and every referenced configuration value.
Direct `kafka://` broker arguments are rejected.

The environment value must be a JSON object with unique property names and a
nonempty string `bootstrap.servers`. Values may be strings, booleans, integers,
or finite floating-point numbers. Null, arrays, nested objects, duplicate keys,
and non-finite numbers are rejected. The CLI rejects `plugin.library.paths`,
removes `debug` and `aws_debug`, and forces `log_level=0` so caller configuration
cannot enable native diagnostic output that bypasses Pollard's safe labels.
Callback-based OAuth, custom loggers, plugins, token providers, and other
non-JSON client configuration remain Python API concerns.

Environment indirection keeps configuration out of the supported command
arguments, but an environment variable is not a secret manager. Inject the JSON
through the deployment's credential mechanism rather than committing it or
typing a production secret into shell history.

For observation and merge sources, the CLI constructs
`KafkaStore(read_only=True)`. It validates the existing topic and complete
retained log but creates no producer, publishes no event, and refuses `put()`
and `update_meta()`. Kafka is supported by `show`, `report`, `verify`, `seal`,
`export`, and `runs`, and as a `merge` source.

A Kafka merge destination is deliberately narrower. It is
existing-and-populated only: full replay must materialize at least one node and
prove that every retained event belongs to the explicit store id. A missing,
empty, wrong-identity, corrupt, truncated, or incompatibly configured topic
fails before producer construction and publishes nothing. Pollard never creates
topics, and `--initialize-if-missing` is invalid for Kafka. Provision the topic
with an administrator, then seed its first valid node through a reviewed Python
`KafkaStore` writer before using it as a CLI destination.

The CLI may validate the destination selector's syntax first, but does not read
the referenced configuration. It fully traverses and validates every source
into a private disk-backed spool, closes each source, and validates all
finalized spools before it reads the destination configuration environment
variable or constructs a Kafka client. Only after the destination topic,
configuration, complete history, and retained prefix validate does it construct
the producer. That producer uses `acks=all`,
idempotence, deterministic operation ids, and replay confirmation. Merge
appends node and metadata commands one at a time, not in a Kafka transaction.
Accepted events are irreversible through Pollard, while an exact rerun
publishes no new events. Quiesce source and destination writers, inspect and
rerun after a partial failure, then verify and seal. Concurrent destination
writers can race mutable merge metadata.

Kafka is not an import or garbage-collection target. Those target forms are
rejected before Pollard reads the configuration environment variable or treats
the argument as a filesystem path.

An observational principal needs topic DESCRIBE, DESCRIBE_CONFIGS, and READ for
the dedicated topic. Pollard directly assigns partition zero and never joins
for balancing or commits consumer offsets. Librdkafka may nevertheless issue
`FindCoordinator`; on ACL-enabled deployments grant GROUP DESCRIBE for:

```text
pollard-observer-<sha256(topic + NUL + store_id)>
```

The digest input is the UTF-8 topic, one zero byte, and the UTF-8 store id. The
group name is deterministic so that permission can be narrow. It is not a
recovery checkpoint and no group offset is stored by Pollard. The observer does
not need topic WRITE, CREATE, ALTER, or DELETE, or cluster IDEMPOTENT_WRITE.
Confirm the exact mapping on the selected managed service.

A CLI destination uses the writer principal described above: topic metadata and
configuration description, READ and WRITE for the dedicated topic, the cluster
permission needed by an idempotent producer, and narrowly scoped GROUP
DESCRIBE for
`pollard-reader-<sha256(topic + NUL + store_id)>`. It still needs no CREATE,
ALTER, or DELETE permission because topic lifecycle remains an administrator
operation.

Pollard disables topic auto-creation for both paths; the read-only source also
disables client metrics push. A `FindCoordinator` request on a completely
pristine Kafka cluster can still cause the broker to initialize its own
`__consumer_offsets` infrastructure. This is not a Pollard topic or event
write, but it is broker-owned cluster state. Pre-provision that internal
infrastructure or use an established cluster when the acceptance boundary
forbids even broker-internal first-use initialization.

## Replay And Integrity

Source construction and reconnect both:

1. fetch topic metadata without requesting a named topic that could be
   auto-created;
2. validate the partition and retention contract;
3. assign partition zero at the beginning;
4. confirm the broker's low watermark is zero; and
5. validate and apply every canonical event in offset order.

Destination construction performs the same checks, rejects a replay that
materializes no node, and constructs the producer only after validation
finishes.

Replay refuses an unexpected store key, topic, partition, offset gap, event
version, envelope field, operation digest, node identity, or noncanonical JSON.
A duplicate command with the same deterministic operation id is safe. A
changed command is a different operation and is evaluated against the replayed
state.

For `read_only=True`, construction records the exclusive high watermark and
replays the committed prefix `[0, high)`. Every later read on that store object
uses the same frozen in-memory view even if writers append. `reconnect()`
atomically validates replacement clients and captures a new prefix only when
every previously observed key/value record digest still appears at its original
offset. If replacement validation, replay, or that prefix check fails, the
replacement clients close and the previous clients and view remain installed.

One dedicated topic is one logical Pollard store and authorization boundary.
`store_id` is repeated in each message key and event envelope as an integrity
marker, not as a multiplexed namespace. A wrong store id fails when the first
retained event is checked. An empty topic contains no event capable of proving
which store id was intended, so an observer can only report that it has no
runs and the CLI refuses to use it as a merge destination. Never put two store
ids or unrelated producers in one topic.
Transactional or external producers are unsupported: aborted transactions and
control-record offsets are not Pollard commands, so replay fails closed on the
resulting invisible offset or timeout.

After producer acknowledgement, KafkaStore consumes through that exact offset
before returning. If confirmation fails, it rebuilds once. An acknowledged
operation that cannot be confirmed is an integrity incident and must stop
writes until full replay succeeds.

## Timeouts And Delivery

`timeout` bounds Pollard metadata, configuration, watermark, poll, and
delivery-confirmation waits. For a read-only CLI source, it also bounds the
complete cold replay from offset zero; increase `timeout=SECONDS` when a valid
retained log cannot be rebuilt in 30 seconds. Configure finite librdkafka
socket, request, and metadata timeouts that fit the application deadline.
Allow enough time for leader election and ISR recovery without creating an
unbounded caller retry.

KafkaStore makes at most two production attempts with the same producer object
and operation id while checking the log between attempts. Do not wrap a failed
write in a retry that changes the node, metadata patch, topic, or `store_id`.

Broker and topic message-size limits must exceed the largest canonical Pollard
event. Test this with the application's maximum expected payload after
redaction.

## Monitoring

Alert on:

- under-replicated partitions, offline partitions, and ISR below the declared
  minimum;
- unclean leader election or unexpected topic configuration changes;
- low watermark greater than zero;
- produce, acknowledgement, authentication, and authorization errors;
- replay offset gaps, malformed events, or unexpected store keys;
- broker disk capacity and topic byte growth;
- cold-start replay time and process memory growth; and
- any command acknowledged without replay confirmation.

KafkaStore uses direct assignment and does not commit consumer-group offsets.
Monitor partition log start and end offsets rather than treating group lag as
its recovery checkpoint.

A CLI observer is internally consistent without stopping writers because it
uses one frozen high-watermark prefix. When a verification, seal, or export must
represent a drained final topic boundary, stop writers and independently record
the topic, partition zero, low watermark zero, and exclusive high watermark.
Pollard's node seal does not contain Kafka offsets.

A CLI destination validates one replay boundary before producer construction,
then synchronizes current history before each append. That is not a
command-wide transaction. Record the initial and final high watermarks when the
transfer needs an independently auditable event boundary.

## Backup, Restore, And Disaster Recovery

Replication inside one Kafka cluster is not an independent backup. Any backup,
snapshot, or mirrored topic used for recovery must preserve every event in
order from the beginning. A destination with a missing prefix is rejected by
the low-watermark and event checks.

Before changing recovery targets:

1. stop all writers for the source topic;
2. record the final high watermark and external seals for required roots;
3. copy and verify the complete ordered log and topic configuration;
4. open a new KafkaStore on the isolated target;
5. compare roots, `verify()` reports, and external seals;
6. strictly replay representative runs; and
7. update all writers together so one logical store never spans two topics.

Deleting the topic is the only complete physical deletion operation at this
backend boundary. It removes every run and is outside Pollard's `gc()` API.

`pollard export` and CLI merge project the materialized Pollard tree. A Kafka
source contributes the tree from its frozen prefix; a Kafka destination appends
new commands representing that projection. Neither path preserves the original
Kafka topic, configuration, offsets, event ordering, duplicate operations,
operation ids, or metadata-patch history. A subtree export or Kafka-to-Kafka
merge is therefore not a Kafka log backup. It also contains retained payload,
result, and metadata content even though ordinary `show` output is content-free
by default.

The CLI retains each fully traversed and validated merge source in its own
private disk-backed spool and validates every finalized spool before reading
the destination configuration or opening the destination, so an invalid source
cannot create a producer or publish an event. This bounds aggregate preparation
RAM while using temporary disk proportional to all sources. Spools contain full
node content and cleanup is attempted on every exit. Cleanup failure can leave
private artifacts and makes an otherwise successful command fail. Combined
failures preserve their primary attribution and add a fixed cleanup notice.
Creation, serialization, disk-write, finalization, corruption, or truncation
fail before destination access. Merge is not a
cross-backend or multi-event transaction, however; a failure while copying, or
cleanup failure after application, can retain changes already accepted by the
destination. For Kafka those changes are append-only and can be removed only by
deleting the whole topic outside Pollard.

## Credential Rotation

Grant the replacement principal first and validate metadata, configuration
description, read, and idempotent write on a non-production topic. Drain
writers, construct a new KafkaStore with the new caller-owned configuration,
confirm complete replay, and replace the old instance. Revoke the old
principal only after a test append and external seal comparison.

## Production Acceptance

Before enabling provider traffic, test:

- concurrent writers preserve one ordered topic history;
- duplicate produce acknowledgement and process restart;
- broker-leader failover with one broker unavailable;
- writes fail when ISR drops below `min.insync.replicas`;
- unclean leader election remains disabled;
- authentication and certificate rotation;
- full replay from offset zero at expected production size;
- maximum event size;
- CLI refusal of missing, empty, wrong-identity, corrupt, truncated, and
  incompatible destination topics before producer construction;
- source-preflight failure without destination client construction or events;
- multiple large sources use distinct private disk spools with bounded
  preparation state and deterministic replay;
- corrupt or truncated spool refusal before destination configuration lookup;
- spool cleanup after success, source failure, constructor failure, and
  destination-body failure, including Windows-safe handle closure;
- an exact CLI merge rerun that leaves the topic high watermark unchanged;
- partial CLI merge recovery by exact rerun, verification, and sealing;
- recovery into a separate cluster; and
- strict replay, `verify()`, and external seal comparison.

Official references: [Apache Kafka design](https://kafka.apache.org/documentation/#design),
[Kafka configuration](https://kafka.apache.org/documentation/#configuration),
[Confluent Python client API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html),
and [Confluent ACL operations](https://docs.confluent.io/platform/current/security/authorization/acls/manage-acls.html).
