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

The principal needs topic metadata and configuration description, READ and
WRITE for the dedicated topic, and the broker permission required for an
idempotent producer. Grant no ALTER, DELETE, or CREATE permission to the normal
application identity. Use a separate administrator for provisioning.

## Replay And Integrity

Construction and reconnect both:

1. fetch topic metadata without requesting a named topic that could be
   auto-created;
2. validate the partition and retention contract;
3. assign partition zero at the beginning;
4. confirm the broker's low watermark is zero; and
5. validate and apply every canonical event in offset order.

Replay refuses an unexpected store key, topic, partition, offset gap, event
version, envelope field, operation digest, node identity, or noncanonical JSON.
A duplicate command with the same deterministic operation id is safe. A
changed command is a different operation and is evaluated against the replayed
state.

After producer acknowledgement, KafkaStore consumes through that exact offset
before returning. If confirmation fails, it rebuilds once. An acknowledged
operation that cannot be confirmed is an integrity incident and must stop
writes until full replay succeeds.

## Timeouts And Delivery

`timeout` bounds Pollard metadata, configuration, watermark, poll, and
delivery-confirmation waits. Configure finite librdkafka socket, request, and
metadata timeouts that fit the application deadline. Allow enough time for
leader election and ISR recovery without creating an unbounded caller retry.

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
- recovery into a separate cluster; and
- strict replay, `verify()`, and external seal comparison.

Official references: [Apache Kafka design](https://kafka.apache.org/documentation/#design),
[Kafka configuration](https://kafka.apache.org/documentation/#configuration),
[Confluent Python client API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html),
and [Confluent ACL operations](https://docs.confluent.io/platform/current/security/authorization/acls/manage-acls.html).
