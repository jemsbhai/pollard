# Documented Limitations

This inventory consolidates limitations stated across the README, operations
guides, API documentation, docstrings, warnings, examples, and tests. A safety
boundary is not a defect: Pollard keeps these cases explicit and fail closed.

| Area | Classification | Limit and supported response |
|---|---|---|
| Tokenmaster defaults | Actionable ergonomic limitation | Governance is optional and `Runtime()` keeps its historical meters. Use `tokenmaster_governance_meters(...)` explicitly, or construct both Tokenmaster meters directly. |
| Kafka test topic setup | Actionable diagnostic limitation | Topic creation acknowledgement can precede metadata visibility. The live test setup now waits for visible, partitioned topic metadata. |
| Lease-loss tests | Actionable diagnostic limitation | Short fixed sleeps were scheduler-sensitive. Tests now synchronize on an attempted renewal, and `ReservationLeaseLost.detail` exposes the recorded reason. |
| Strict replay | Intentional safety boundary | Replay returns the stored result and never calls a provider, tool, current policy hook, or meter settlement path. It reproduces Pollard-observed history, not hidden provider state. |
| Revalidation | Provider/backend constraint | A live comparison can measure visible output drift but cannot prove a hosted provider preserved internal execution state. Comparator code remains caller-trusted and can emit sensitive details if written to do so. |
| Provider calls | Provider/backend constraint | A completed or ambiguously dispatched external call cannot be undone. Mark post-dispatch uncertainty so Pollard settles conservatively and prevents unsafe blind retry. |
| Budgets | Intentional safety boundary | Budgets are application-side limits, not provider billing caps. Estimates may settle above a reservation after provider work occurs; exact step and request-window meters remain exact. |
| Token usage | Provider/backend constraint | Missing provider usage cannot be reconstructed reliably. Compatible usage is recorded; otherwise token settlement warns rather than inventing a provider count. |
| Tokenmaster pricing | Provider/backend constraint | Pollard uses the installed offline registry. It does not scrape prices or silently refresh profiles. Non-request-derived charges such as token-hour cache storage fail closed. |
| Model aliases | Documentation-only ambiguity | Azure deployment names and gateway routes cannot prove an underlying model. Bind `model="provider:model"` explicitly; a provider-returned name does not replace an explicit binding. |
| Token estimation | Provider/backend constraint | Tokenizer output is an estimate. GPT-5-family identifiers fall back to `o200k_base` when needed; provider usage remains authoritative. |
| Tool schemas | Intentional safety boundary | The registry supports a documented zero-dependency JSON Schema subset and rejects unsupported keywords, types, references, or ambiguous unions rather than validating partially. |
| Tool dispatch | Intentional safety boundary | A registry-installed runtime cannot execute an arbitrary caller function. Unknown or mismatched tools, invalid arguments, missing confirmation, and policy denial are refusals. |
| Redaction | Intentional safety boundary | Unknown actions lack a trusted schema, so Pollard cannot infer sensitive fields. Register schemas and annotations; unsupported or unsafe cases fail closed. |
| Audit integrity | Intentional safety boundary | Trees and seals are tamper-evident, not tamper-proof. They detect changed retained history but cannot prevent deletion or coordinated replacement of data and custody material. Mutable charge/timing metadata is outside node identity. |
| Garbage collection | Intentional safety boundary | Offline GC is available only on backends with the required traversal/deletion contract. Kafka has no record-level GC, and the CLI keeps import/GC SQLite-only. |
| Shared arbitration | Intentional safety boundary | All workers under one shared limit must use the same transactional backend and logical store id. Pollard is not consensus across disconnected databases. Memory, HashRope, and Kafka remain per-runtime. |
| SQLite | Provider/backend constraint | Shared arbitration is limited to processes sharing one database file on one host. |
| Redis | Provider/backend constraint | Durability depends on persistence, replication, eviction, and failover policy. Routing cannot recover when no seed can discover a primary, and the CLI cannot express every caller-owned option. |
| MongoDB | Provider/backend constraint | Transactions require a replica set or sharded deployment. Index DDL is separate from logical initialization and partial states fail closed rather than being repaired. |
| Neo4j | Provider/backend constraint | Each logical store serializes through a coordinator, favoring exact accounting over maximum throughput. Global DDL is separate from logical initialization and partial states fail closed. |
| PostgreSQL leases | Provider/backend constraint | Renewal depends on database availability and scheduling. Size leases above expected stalls; a lost lease is recorded after settlement and must not trigger provider retry. |
| Kafka store | Provider/backend constraint | Kafka supplies an ordered Store log, not compare-and-swap arbitration. It requires one partition and infinite retention; replay/memory grow with the log. Read-only stores freeze a prefix until reconnect. |
| Kafka identity | Intentional safety boundary | An empty topic cannot prove `store_id`; Pollard does not create topics or seed identity for CLI merge destinations. Populated history must prove identity before producer construction. |
| Kafka delivery | Provider/backend constraint | Acknowledgement loss can make outcome uncertain. Retry only the identical operation; changing node, metadata, topic, or store id fails closed. |
| CLI credentials | Intentional safety boundary | Mutating remote destinations require explicit environment-backed selectors. Direct credential-bearing forms are restricted or refused where safe redaction and initialization cannot be guaranteed. |
| Remote initialization | Intentional safety boundary | Destinations are existing-only unless the supported backend receives explicit `--initialize-if-missing`. Sources are prepared before destination access; ambiguous partial state is never auto-repaired. |
| Merge atomicity | Provider/backend constraint | Cross-node and cross-backend merge is not one transaction. Preparation is fail closed and identical retries are idempotent, but a destination may contain a validated prefix after failure. |
| Observability callbacks | Intentional safety boundary | `on_node` and telemetry run after safe storage. Callback failure warns and cannot discard the node; external telemetry delivery is best effort. |
| Energy meter | Provider/backend constraint | NVML measures the whole GPU, including other processes, and is unsuitable for strict per-call attribution. |
| Optional integrations | Intentional safety boundary | Provider SDKs, stores, estimators, telemetry, Tokenmaster, and framework recipes remain extras. Core import and offline replay do not require them. |
| Framework wrappers | Documentation-only ambiguity | A wrapper around one complete agent run cannot see internal model/tool calls. Use the deeper recipe integration when each call must be governed and recorded. |
| Examples and evidence | Documentation-only ambiguity | Environment-specific re-recording can change IDs, timings, and provider output. Offline evidence demonstrates the stated workflow only, not general autonomy or provider reproducibility. |
| Data deletion | Provider/backend constraint | Deleting a local record cannot undo a provider call or delete provider-side data. Retention, backups, and deletion for remote stores remain operator responsibilities. |
| Python support | Intentional compatibility boundary | Pollard supports Python 3.10 and newer; optional backends additionally depend on compatible releases of their own SDKs and services. |

Warnings about missing compatible usage or post-dispatch Tokenmaster diagnostics
are deliberate visibility, not silent fallback. Credential-bearing backend
errors and labels are sanitized. For remediation details, see
`troubleshooting.md`, `provider-boundary-hardening.md`, and the backend-specific
operations guides.
