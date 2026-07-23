# Pollard documentation

This index maps each operational question to one authoritative document. The
root [project README](https://github.com/jemsbhai/pollard/blob/main/README.md)
is the install and feature overview. The documents below provide the contracts,
failure boundaries, and complete commands needed to operate or review Pollard.

## Start here

| Goal | Document |
|---|---|
| Run a credential-free example | [Offline examples](https://github.com/jemsbhai/pollard/blob/main/examples/README.md) |
| Use OpenAI, Anthropic, Azure, Bedrock, another cloud, or an agent framework | [Live recipes](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md) |
| Choose a direct cloud adapter or LiteLLM route | [Cloud-hosted providers](https://github.com/jemsbhai/pollard/blob/main/docs/cloud-providers.md) |
| Review provider failure classifications, conservative accounting, and the cloud ledger | [Provider boundary hardening](https://github.com/jemsbhai/pollard/blob/main/docs/provider-boundary-hardening.md) |
| Inspect, verify, report, seal, or export a run | [Observability](https://github.com/jemsbhai/pollard/blob/main/docs/observability.md) |
| Diagnose refusals, replay misses, integrity, providers, or stores | [Troubleshooting](https://github.com/jemsbhai/pollard/blob/main/docs/troubleshooting.md) |
| Classify, redact, retain, or delete recorded data | [Data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md) |
| Share budgets across processes or hosts | [Scale-out stores](https://github.com/jemsbhai/pollard/blob/main/docs/scale-out.md) |
| Run a configured PostgreSQL, Redis, MongoDB, Neo4j, or Kafka example | [Distributed-store walkthrough](https://github.com/jemsbhai/pollard/blob/main/examples/README.md#configured-distributed-store-walkthrough) |
| Migrate, back up, restore, or reconnect PostgreSQL | [PostgreSQL operations](https://github.com/jemsbhai/pollard/blob/main/docs/postgres-operations.md) |
| Operate Redis, MongoDB, Kafka, or Neo4j stores | [Distributed store operations](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md) |
| Deploy and recover Redis, including caller-owned Sentinel discovery | [Redis operations](https://github.com/jemsbhai/pollard/blob/main/docs/redis-operations.md) |
| Deploy and recover a MongoDB replica-set or sharded store | [MongoDB operations](https://github.com/jemsbhai/pollard/blob/main/docs/mongodb-operations.md) |
| Deploy and recover a routed Neo4j store | [Neo4j operations](https://github.com/jemsbhai/pollard/blob/main/docs/neo4j-operations.md) |
| Provision and recover a Kafka audit topic | [Kafka operations](https://github.com/jemsbhai/pollard/blob/main/docs/kafka-operations.md) |
| Review backend classifications, tests, spend, and remaining limits | [Store backend validation](https://github.com/jemsbhai/pollard/blob/main/docs/store-backend-validation.md) |
| Understand exactly what a subtree seal covers | [Seal design](https://github.com/jemsbhai/pollard/blob/main/docs/seal.md) |
| Depend on the stable 1.x contracts | [API stability policy](https://github.com/jemsbhai/pollard/blob/main/docs/api-stability.md) |
| Look up runtime, run, budget, registry, store, async, adapter, and exception APIs | [Public API reference](https://github.com/jemsbhai/pollard/blob/main/docs/api-reference.md) |
| Reproduce a published claim | [Evidence index](https://github.com/jemsbhai/pollard/blob/main/evidence/README.md) |
| Review historical release messaging and claim limits | [Launch history](https://github.com/jemsbhai/pollard/blob/main/docs/launch.md) |
| Prepare and publish a release | [Local-only release runbook](https://github.com/jemsbhai/pollard/blob/main/docs/releasing.md) |

## Scope and trust boundaries

Pollard owns the execution ledger, budget and policy checks, replay behavior,
registry resolution, and built-in store implementations. The calling
application owns prompts, model and tool selection, provider clients,
credentials, retries, provider account limits, and the safety of side effects.

The repository separates three kinds of runnable material:

- `examples/01_*` through `08_*` are small, offline walkthroughs.
- `examples/exp_*` are controlled evidence runners with their own protocols and
  prerequisites. They do not call hosted model providers.
- `docs/recipes/*.py` are opt-in integration recipes. Provider-backed recipes
  make live requests and can incur charges. CI compiles them but never executes
  a provider request.

## Documentation rules

- Commands are written for PowerShell and run from the repository root unless
  a section says otherwise.
- Every README link is an absolute HTTPS URL so it also works on PyPI.
- Examples name their network, credential, cost, storage, and expected-output
  behavior.
- Claims that include measured numbers link to a committed evidence protocol.
- Secrets belong in provider SDK configuration or environment variables, never
  in Pollard payloads, run labels, results, metadata, or committed files.
