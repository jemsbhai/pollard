# Troubleshooting

Start with the content-free CLI. It exposes topology, charges, refusals, and
integrity without printing prompts, tool arguments, or results:

```powershell
pollard runs runs.db --json
pollard show runs.db <root-id>
pollard report runs.db <root-id> --json
pollard verify runs.db <root-id> --json
```

Use `--payloads` only in an approved destination. Pollard recordings can
contain the full model request and result even though default CLI output hides
them.

## BudgetExceeded

`BudgetExceeded` means a precheck refused a model or tool step and recorded a
refusal node. The exception's `refusal_id` identifies it. Inspect that node and
the charge report. Common causes are:

- an exact step, depth, or request-window limit has no capacity remaining;
- an input-token estimate plus reserved output exceeds the active token budget;
- another process holds a shared transactional reservation;
- a resumed run reuses the same root-scoped budget or request window; or
- the runtime and application disagree about which logical store is shared.

The refused callable did not run. This guarantee does not apply to a completed
provider call whose actual usage settles above an estimate; that external spend
has already occurred, and later steps are refused.

For transactional stores, inspect renewal errors and database interruptions.
Calls renew their reservation while running. Do not manually edit arbiter
tables.

`ReservationUncertain` means a reserve or release transaction could not be
confirmed after reconnect. `SettlementUncertain` means the provider callable
completed but its database settlement could not be confirmed. Do not repeat
the provider call. Use the reservation ID and the recovery procedure in
[PostgreSQL operations](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md).

A `call_outcome_unknown` note means a direct adapter or generic callable
reported that dispatch occurred but the external result was not known. A
`call_recording_failed` note means the callable returned, but local meter or
result processing failed before a replayable result could be stored. Both notes
contain a blocked payload digest and error type, not the prompt, result, provider
message, or raw response. Treat either note as consumed external capacity and do
not retry automatically.

## PolicyViolation or ConfirmationRequired

For a registry refusal, confirm all of these values:

- tool name and version match one `ActionSpec` exactly;
- the action schema uses Pollard's supported JSON Schema subset;
- required fields are present and no forbidden extra field is supplied;
- policy state permits the call;
- side-effectful actions have the required confirmation; and
- replay uses the same registry digest and redacted identity payload.

`ConfirmationRequired` carries a resume token. Treat it as a capability: do not
log it in an untrusted destination. A dry run records the intended action but
does not execute a side-effectful handler.

## MissingRecording

Replay never falls back to a live call. `MissingRecording` means the computed
node ID was not present with a stored result. Compare the recorded and current:

- parent node ID;
- node kind and attempt number;
- complete payload, including model, prompt, tools, provider metadata, and
  reserved `_pollard` fields; and
- registry name, version, schema, policy state, and redaction marker where
  applicable.

Use hybrid mode only during deliberate recording. CI should use replay mode and
must not receive provider credentials.

## IntegrityError or verify findings

Stop using the recording as evidence. Do not repair node IDs or result digests
by editing the database. Preserve a read-only copy, capture `pollard verify
--json`, and compare against an independently stored seal or source artifact.

An identity finding means the node's stored parent, kind, attempt, or payload no
longer hashes to its ID. A result finding means result text no longer hashes to
its digest. Missing parents, traversal anomalies, or an export seal mismatch can
also indicate an incomplete transfer.

Restore from a trusted backup or re-record under a new evidence artifact. A new
seal over changed data does not prove that the old recording was valid.

## Provider authentication, model, and quota errors

Pollard passes provider exceptions through because the caller owns the SDK.
Before retrying, check:

- the correct environment, profile, workload identity, or token provider is
  active;
- the credential is scoped to the intended account, project, resource, or
  workspace;
- endpoint, Region, model ID, deployment name, and API version agree;
- the model is enabled and available in that Region or project;
- the principal has inference and any separate token-count permission;
- provider quotas, rate limits, spending limits, and remaining credit permit
  the call; and
- SDK, framework, proxy, and gateway retries are disabled or explicitly
  budgeted.

Do not paste a credential into a model payload to test it. Use the provider's
credential diagnostic outside Pollard, then rerun with the same capped prompt.

## Unexpected duplicate provider calls

Check every layer that can retry or issue an internal request:

- provider SDK retries;
- HTTP transport retries;
- LiteLLM or gateway retries;
- agent framework model or validation retries;
- a token-count request used during precheck;
- a hybrid cache miss caused by changed identity; and
- an application loop that submits the same logical work under a different
  parent or attempt number.

Pollard never retries a step function on its own. A node is stored after a
successful function result or completed stream. Provider errors are not turned
into successful cached results.

## SQLite locked or PostgreSQL unavailable

SQLite serializes writers and is intended for one host. Keep transactions
short, avoid network filesystems, and use PostgreSQL for several hosts or
sustained contention. Do not run `pollard gc` while another process writes the
same store.

For PostgreSQL, verify DSN reachability, TLS, database and schema permissions,
first-use create privileges, sequence use, and matching `store_id`. The
preferred CLI form is `pg-env:VARIABLE#store-id` so a password does not appear
in process arguments.

After a server restart or backend termination, create a new `PostgresStore` or
call `reconnect()` on the existing instance. A schema migration requirement or
unknown schema version is intentional refusal, not a connectivity error.

The same `reconnect()` rule applies to Redis, MongoDB, Neo4j, and Kafka. Redis
requires persistent no-eviction storage. MongoDB refuses a standalone server
because it cannot run the required transactions. Neo4j writes and reads are
routed to a primary. Kafka refuses multiple partitions, compaction, finite
retention, a truncated log, malformed events, or a changed store key. See the
[distributed store runbook](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
for exact configuration checks and recovery steps.

## Remote Store Refusal Or Uncertain Outcome

Treat schema, topology, and topic-configuration errors as intentional
fail-closed checks:

- Redis needs an intact store identity and revision. Confirm persistence,
  `maxmemory-policy noeviction`, the URL, prefix, and `store_id`.
- MongoDB needs a replica set or sharded deployment. Do not use
  `directConnection=true` as a production substitute for normal topology
  discovery.
- Neo4j needs write routing, access to the selected database, and permission to
  create or use the two Pollard uniqueness constraints.
- Kafka needs one pre-created topic, exactly one partition, delete-only cleanup,
  infinite time and byte retention, and history beginning at offset zero.

`ReservationUncertain` and `SettlementUncertain` mean the server may have
committed even though the client could not confirm it. Stop new dispatch for
that logical store and reconcile the same reservation id and exact request or
charges. Do not create a replacement id or release capacity based only on a
transport error. Account for an ambiguous dispatched call at its reserved
ceiling until reconciliation succeeds.

`ReservationLeaseLost` means a completed call outlived its last confirmed
lease. The call and available accounting evidence remain recorded, but the
shared precheck guarantee no longer covers that interval. Investigate database
latency, failover, worker scheduling, and lease duration before resuming.

## Import or merge failure

Import verifies the full subtree before writing. Confirm the JSON is complete,
the seal report matches, and any external parent already exists in the target.

Merge rejects unequal identity fields under the same node ID. In default mode,
result or metadata conflicts are retained in destination metadata; in
`--replay` mode, a result conflict is an integrity error. Repeating a successful
merge is idempotent.

## CLI exit codes

- `0`: command completed, or verification found no integrity problem.
- `1`: verification completed and reported one or more findings.
- `2`: invalid command, unreadable input, missing node, optional dependency
  error, or another handled Pollard or operating-system error.

In automation, consume `--json` output and the exit code. Human-readable tree
formatting is not a stable machine interface.

## Diagnostic bundle

When opening an issue, include:

- Pollard and Python versions;
- operating system and store backend;
- redacted install and invocation commands;
- provider and model or deployment name, if relevant;
- content-free `runs`, `show`, `report --json`, and `verify --json` output;
- the exception type and message; and
- the smallest offline reproducer or frozen provider response fixture.

Do not include API keys, tokens, DSNs, prompts, results, customer data, signed
URLs, resume tokens, or a database file unless the issue channel is approved
for that data.
