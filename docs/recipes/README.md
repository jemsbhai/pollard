# Pollard integration recipes

These twelve scripts place Pollard around an existing provider client,
framework workflow, graph node, or MCP session. Pollard never creates a cloud
account, chooses a provider deployment, or reads a provider credential. The
surrounding SDK owns authentication and transport; Pollard owns the governed
semantic step and its local audit record.

The LangChain incident-response, LangChain support-policy RAG, Pydantic refund,
and pydantic-ai claim-triage recipes run deterministic local paths by default.
They need no credential or network connection. The two LangChain recipes and
pydantic-ai make hosted OpenAI requests only when `--live` is passed; the
Pydantic refund workflow has no live mode.

Run commands from the repository root. Install the repository in editable form
while changing the recipes, or replace `-e` with a released Pollard version.

## Safety and cost contract

The provider-backed recipes and explicit `--live` paths are live. The four
default-offline workflow recipes are exercised without provider access.
Importing or compiling a recipe makes no request, and `--help` on a recipe with
a CLI makes no request.

Before any live run:

1. Read the script and confirm its model, endpoint, prompt, tool, and output
   limit.
2. Set a provider-side account or project spending limit. A Pollard token budget
   is not a dollar-denominated provider billing limit.
3. Use a least-privilege credential and a non-production resource when
   possible.
4. Start with a fresh `.db` path, or understand that `mode="hybrid"` reuses an
   exact existing node but calls the provider when identity changes.
5. Inspect the result and usage immediately after the run.

The checked-in direct-provider calls disable SDK retries and cap each response
at 128 output tokens. The OpenAI and Anthropic tool loops can make two model
requests; the other direct hosted recipes make one. An agent framework may make
more than one request in `--live` mode. Input tokens, provider-side tool
charges, regional pricing, and framework-internal requests still count. These
scripts cannot guarantee that a run stays under a particular dollar amount. A
user with only a small credit balance should enforce that limit in the provider
console and run one recipe at a time.

Never put an API key, access token, DSN, customer secret, or signed URL in a
model payload. Pollard stores model payloads and results in its SQLite ledger.

## Recipe matrix

| Script | Boundary demonstrated | Install | Credential or endpoint | Live requests | Recording |
|---|---|---|---|---:|---|
| `openai_tool_loop.py` | OpenAI Responses tool calls through a Pollard registry | `pip install -e ".[openai]"` | `OPENAI_API_KEY`; optional `POLLARD_OPENAI_MODEL` | Up to 2 | `openai-tool-loop.db` |
| `anthropic_tool_loop.py` | Anthropic Messages tool use plus token precheck | `pip install -e ".[anthropic]"` | `ANTHROPIC_API_KEY`; optional `POLLARD_ANTHROPIC_MODEL` | Up to 2 plus token counts performed by the SDK adapter | `anthropic-tool-loop.db` |
| `azure_openai.py` | Azure OpenAI v1 through the OpenAI Responses adapter | `pip install -e ".[azure-openai]"` | Endpoint and deployment plus API key or DefaultAzureCredential | 1 | `azure-openai.db` |
| `bedrock_converse.py` | Amazon Bedrock Converse through a boto3 client | `pip install -e ".[bedrock]"` | AWS SDK credential chain, Region, model access, IAM permission | 1, plus optional CountTokens | `bedrock-converse.db` |
| `litellm_cloud.py` | Vertex AI, Azure AI, SageMaker, OCI, Watsonx, Databricks, Bedrock, or another LiteLLM route | `pip install -e ".[litellm]"` plus route dependencies | Selected LiteLLM provider configuration | 1 | `litellm-cloud.db` |
| `langgraph_node.py` | Pollard inside one LangGraph node | `pip install -e ".[langgraph]"` | `OPENAI_API_KEY`; optional `POLLARD_OPENAI_MODEL` | 1 | `langgraph-node.db` |
| `langchain_support_rag.py` | Local return-policy RAG through a registered LangChain retriever and typed answer | `pip install -e ".[langchain]"` | None by default; `OPENAI_API_KEY` for `--live`; optional `POLLARD_OPENAI_MODEL` | 0 by default; 1 with `--live` | `langchain-support-rag.db` |
| `langchain_incident_response.py` | Typed LangChain incident triage with governed model and action boundaries | `pip install -e ".[langchain]"` | None by default; `OPENAI_API_KEY` for `--live`; optional `POLLARD_OPENAI_MODEL` | 0 by default; 2 with `--live` | `langchain-incident.db` |
| `pydantic_refund_workflow.py` | Pydantic-generated action schema, preview, confirmation, redaction, and replay | `pip install -e ".[pydantic]"` | None | 0 | `pydantic-refund.db` |
| `pydantic_ai_claim_triage.py` | Typed claim triage with every pydantic-ai model request and registered tool execution governed | `pip install -e ".[pydantic-ai]"` | None by default; `OPENAI_API_KEY` for `--live`; optional `POLLARD_OPENAI_MODEL` | 0 by default; 2 with `--live` | `pydantic-ai-claim.db` |
| `pydantic_ai_wrap.py` | One complete pydantic-ai run as a Pollard step | `pip install -e ".[pydantic-ai]"` | `OPENAI_API_KEY`; optional `POLLARD_OPENAI_MODEL` | 1 unless the agent stack is changed | `pydantic-ai.db` |
| `mcp_registry.py` | MCP tool discovery and invocation through the registry firewall | `pip install -e ".[mcp]"` | None for local stdio; server-specific auth for HTTP | 1 MCP session and tool call | `mcp-registry.db` |

Provider models and SDKs change independently of Pollard. The defaults and
framework paths were checked against primary documentation on 2026-07-27. Pin
dependencies and model IDs for production, then retest before upgrading.

## OpenAI Responses tool loop

The recipe asks the model for a weather tool call, resolves that call against a
versioned `Registry`, records the local tool result, and returns a
`function_call_output` item for the final response. It passes `store=False` to
the Responses API and disables SDK retries.

```powershell
python -m pip install -e ".[openai]"
$env:OPENAI_API_KEY = "<project-api-key>"
$env:POLLARD_OPENAI_MODEL = "gpt-5.6"  # optional override
python docs\recipes\openai_tool_loop.py
```

Expected output is a short Boston weather sentence followed by a `pollard show`
command. The tool returns fixed demo data; it does not call a weather service.
The recipe should record a root, two model calls, and one registered tool call.

The default follows OpenAI's
[current model guide](https://developers.openai.com/api/docs/guides/latest-model),
and the exchange follows the official
[function calling guide](https://developers.openai.com/api/docs/guides/function-calling).
OpenAI service storage settings and Pollard's local SQLite recording are
separate data-retention decisions.

## Anthropic Messages tool loop

The Anthropic recipe sends a Messages request with a client tool, parses the
returned `tool_use` block, executes the matching registered action, appends a
`tool_result`, and asks for the final answer. It disables SDK retries, fixes the
response limit at 128 tokens, and explicitly requests low effort.

```powershell
python -m pip install -e ".[anthropic]"
$env:ANTHROPIC_API_KEY = "<workspace-api-key>"
$env:POLLARD_ANTHROPIC_MODEL = "claude-sonnet-5"  # optional override
python docs\recipes\anthropic_tool_loop.py
```

Expected output is a short Boston weather sentence and an inspection command.
The model default is a pinned API model ID, not an evergreen alias. Review the
current [Claude model table](https://platform.claude.com/docs/en/about-claude/models/overview)
and [tool-use guide](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
before changing the ID or effort setting.

## Azure OpenAI v1

Azure OpenAI's v1 endpoint uses the standard `OpenAI` client. The endpoint is
the Azure resource URL, while `model` is the Azure deployment name rather than
the catalog model name.

```powershell
python -m pip install -e ".[azure-openai]"
$env:AZURE_OPENAI_ENDPOINT = "https://<resource>.openai.azure.com"
$env:AZURE_OPENAI_DEPLOYMENT = "<deployment-name>"
$env:AZURE_OPENAI_API_KEY = "<resource-key>"
python docs\recipes\azure_openai.py
```

Expected output is a two-sentence explanation and an inspection command. The
payload uses `_pollard.provider="azure.ai.openai"`; the adapter commits that
metadata to node identity and removes it before the SDK request.

For Microsoft Entra ID, authenticate `DefaultAzureCredential` through the
operator's approved source and use the same recipe without an API key:

```powershell
Remove-Item Env:AZURE_OPENAI_API_KEY -ErrorAction SilentlyContinue
python docs\recipes\azure_openai.py --entra-id
```

Microsoft recommends Microsoft Entra ID or Azure Key Vault instead of a literal
API key for production. The Pollard adapter is unchanged. See Microsoft's
[endpoint and authentication guide](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/switching-endpoints).

## Amazon Bedrock Converse

Pass a model ID or inference-profile ID that is available in the selected AWS
Region. boto3 uses its normal credential chain; the script does not accept
secret keys on the command line.

```powershell
python -m pip install -e ".[bedrock]"
$env:AWS_PROFILE = "<profile>"       # optional when another SDK source is active
$env:AWS_REGION = "us-east-1"        # or pass --region
python docs\recipes\bedrock_converse.py `
  us.amazon.nova-lite-v1:0 `
  --region us-east-1
```

Add `--count-tokens` only after confirming the selected model supports
CountTokens and the principal has that permission:

```powershell
python docs\recipes\bedrock_converse.py `
  us.amazon.nova-lite-v1:0 `
  --region us-east-1 `
  --count-tokens
```

`Converse` needs `bedrock:InvokeModel`; streaming needs
`bedrock:InvokeModelWithResponseStream`; the optional precheck needs
`bedrock:CountTokens`. Model access and Region availability are separate from
IAM. Consult the official [Converse guide](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html),
[CountTokens reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CountTokens.html),
and [SDK credential chain](https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html).

## LiteLLM cloud routes

LiteLLM is the normalized chat path for providers without a direct Pollard
adapter. Install the route's SDK or authentication dependency in addition to
`pollard[litellm]`. The `--provider` value is audit and telemetry metadata; it
does not select or authenticate a route.

```powershell
python -m pip install -e ".[litellm]"

# Google Vertex AI using Application Default Credentials
python docs\recipes\litellm_cloud.py vertex_ai/gemini-2.5-flash `
  --provider gcp.vertex_ai

# Microsoft Foundry Models through an Azure AI inference endpoint
python docs\recipes\litellm_cloud.py azure_ai/<deployment-or-model> `
  --provider azure.ai.inference

# Amazon Bedrock through LiteLLM instead of the direct Converse adapter
python docs\recipes\litellm_cloud.py bedrock/us.amazon.nova-lite-v1:0 `
  --provider aws.bedrock
```

Use the selected route's page in the official
[LiteLLM provider directory](https://docs.litellm.ai/docs/providers). Vertex AI
normally uses [Google Application Default Credentials](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc).
Azure AI, SageMaker, OCI, Watsonx, and Databricks each have distinct endpoint,
identity, deployment, and package requirements; Pollard does not normalize
those credentials.

## LangGraph node

This recipe compiles a one-node `StateGraph`. The graph remains responsible for
state and routing. The node delegates only its provider request to a Pollard
run, so the returned answer and token usage enter the Pollard tree.

```powershell
python -m pip install -e ".[langgraph]"
$env:OPENAI_API_KEY = "<project-api-key>"
python docs\recipes\langgraph_node.py
```

Expected output is one answer and an inspection command. Adding graph nodes
does not automatically govern them; wrap each model or tool boundary that must
appear in the Pollard ledger. The graph shape follows LangGraph's official
[StateGraph example](https://docs.langchain.com/oss/python/langgraph/overview).

## LangChain support-policy RAG

This offline-first retrieval-augmented generation workflow answers a customer
support question from a fixed local return-policy corpus. A LangChain
`BaseRetriever` runs inside the registered `search_policy@1` action, then a
governed model call produces a typed Pydantic `SupportAnswer`. Pollard therefore
records two semantic steps beneath the root: one tool call followed by one
model call.

The CLI defaults to `--database langchain-support-rag.db` and `--mode record`.
Its default question asks whether unopened headphones delivered 35 days ago
can be returned. `--model` defaults to `POLLARD_OPENAI_MODEL` when set, or
`gpt-5.6`, and affects only the `--live` path.

Record the deterministic local result, then replay it with the same question:

```powershell
python -m pip install -e ".[langchain]"
$question = "Can I return unopened headphones that were delivered 35 days ago?"
python docs\recipes\langchain_support_rag.py `
  --database langchain-support-rag.db `
  --mode record `
  --question $question
python docs\recipes\langchain_support_rag.py `
  --database langchain-support-rag.db `
  --mode replay `
  --question $question
```

Expected output is a structured `SupportAnswer` whose citations include the
fixed source ID `returns-electronics`. The expected two-step recording tree is:

```text
workflow root
`-- tool: search_policy@1
    `-- model: typed SupportAnswer
```

Strict replay returns both stored results without executing the retriever
handler or model callable, and reports `avoided.steps=2`. A changed question,
model identity, policy schema, or parent path produces a replay miss rather
than a live request.

Use hybrid mode when exact hits should replay and a miss should run the selected
offline or hosted model. Hosted inference is an explicit opt-in:

```powershell
$env:OPENAI_API_KEY = "<project-api-key>"
$env:POLLARD_OPENAI_MODEL = "gpt-5.6"  # optional override
python docs\recipes\langchain_support_rag.py `
  --database langchain-support-rag.db `
  --mode record `
  --live `
  --model $env:POLLARD_OPENAI_MODEL `
  --question "Can I return unopened headphones that were delivered 35 days ago?"
```

The live path makes one OpenAI model request after local retrieval and can
incur provider charges. The retrieved policy excerpt and question are sent to
the provider, so use only public demo policy text and a question without
customer data or credentials. The Pollard registry still validates
`search_policy` arguments before its local handler executes.

## LangChain incident response

This end-to-end workflow triages an incident, conditionally loads a local
runbook through a registered read-only action, and produces a typed response
plan. Its two model boundaries and runbook lookup enter the Pollard ledger. The
default LangChain pipeline uses a deterministic local model callable, so record
and replay need no API key or network connection.

The CLI defaults to `--database langchain-incident.db` and `--mode record`.
The built-in incident describes a production checkout outage. `--model`
defaults to `POLLARD_OPENAI_MODEL` when set, or `gpt-5.6`, and affects only the
`--live` path.

Use the same incident text for recording and strict replay:

```powershell
python -m pip install -e ".[langchain]"
$incident = "Checkout errors exceed the alert threshold in us-east."
python docs\recipes\langchain_incident_response.py `
  --database langchain-incident.db `
  --mode record `
  --incident $incident
python docs\recipes\langchain_incident_response.py `
  --database langchain-incident.db `
  --mode replay `
  --incident $incident
```

Record mode executes the deterministic framework path and writes the semantic
steps. Replay mode opens the recording read-only, returns those results, and
does not execute the model or action handlers. Hybrid mode reuses exact hits
and executes the selected offline or live backend on a miss.

Hosted inference is an explicit opt-in:

```powershell
$env:OPENAI_API_KEY = "<project-api-key>"
$env:POLLARD_OPENAI_MODEL = "gpt-5.6"  # optional override
python docs\recipes\langchain_incident_response.py `
  --database langchain-incident.db `
  --mode record `
  --live `
  --model $env:POLLARD_OPENAI_MODEL `
  --incident "Checkout errors exceed the alert threshold in us-east."
```

The live path uses the OpenAI Responses API for two model requests, disables
SDK retries and OpenAI response storage, and caps each response at 512 tokens.
It can incur provider charges. Keep the incident free of customer data and
credentials. A replay hit still requires the same semantic inputs, including
the incident, selected model, and workflow configuration.

## Pydantic refund workflow

This local workflow derives a registered refund-action schema from nested
Pydantic models. It validates typed arguments, marks the customer token as a
sensitive string, previews the side effect without executing it, pauses the
recorded action for confirmation, and strictly replays the confirmed result
without issuing a second refund.

```powershell
python -m pip install -e ".[pydantic]"
python docs\recipes\pydantic_refund_workflow.py `
  --database pydantic-refund.db `
  --order-id order-1001 `
  --amount-cents 15000 `
  --customer-token "demo-token-not-a-real-credential"
```

One invocation performs the dry-run preview, confirmed record, and strict
replay. There is no `--live` flag and no network request. The handler receives
the supplied demo value during the confirmed local execution, while the audit
payload stores only Pollard's deterministic redaction marker. Because that
marker remains part of node identity, exact replay requires the same sensitive
input. The confirmed stage uses hybrid mode, so rerunning the exact command
reuses its result after approval instead of executing the handler again. Do not
pass a production credential to this demonstration.

With no arguments, the recipe writes `pydantic-refund.db` and uses order
`ord_1042`, amount `12500` cents, and the non-production token
`tok_demo_customer_1042`. The explicit flags above show how each input can be
changed while keeping the token illustrative.

The generated schema intentionally uses Pollard's supported finite subset:
objects, strings, integers, booleans, arrays, enums, local references, and
standard annotations. Pydantic fields that emit `anyOf`, `number`, `format`,
range, or length keywords require a different schema boundary because Pollard
rejects unsupported validation keywords at registration.

## pydantic-ai claim triage

The claim-triage workflow returns a typed Pydantic result from a pydantic-ai
agent. Its default model is the framework's deterministic `TestModel`. A custom
pydantic-ai capability wraps each model request with `AsyncRun.amodel_call()`
and intercepts the `load_claim` tool through the versioned Pollard registry and
`AsyncRun.atool_call()`. The capability strips run IDs, conversation IDs, and
timestamps from request identity, then restores each recorded `ModelResponse`
for the agent loop.

The path is deliberately sequential: parallel tool calls are disabled, agent
concurrency is one, and the successful workflow contains exactly two governed
model calls and one governed read-only tool call. This is different from
`pydantic_ai_wrap.py`, which records a complete agent run as one opaque model
node.

The CLI defaults to `--database pydantic-ai-claim.db`, `--mode record`, and
claim `clm_2048`. `--model` defaults to `POLLARD_OPENAI_MODEL` when set, or
`gpt-5.6`, and affects only `--live`.

```powershell
python -m pip install -e ".[pydantic-ai]"
$claim = "claim-1001"
python docs\recipes\pydantic_ai_claim_triage.py `
  --database pydantic-ai-claim.db `
  --mode record `
  --claim-id $claim
python docs\recipes\pydantic_ai_claim_triage.py `
  --database pydantic-ai-claim.db `
  --mode replay `
  --claim-id $claim
```

Use `--mode hybrid` when an application should reuse an exact recording and run
the selected backend only on a miss. The strict replay command makes no model
request and does not rerun the local claim lookup. A record output reports
`calls.model=2` and `calls.tool=1`. Replaying the same path reports both live
call counters as zero: `calls.model=0` and `calls.tool=0`. The stored path still
reports `ledger.model_calls=2` and `ledger.tool_calls=1`, while
`report.avoided.steps` is `3`.

The OpenAI-backed path is opt-in:

```powershell
$env:OPENAI_API_KEY = "<project-api-key>"
$env:POLLARD_OPENAI_MODEL = "gpt-5.6"  # optional override
python docs\recipes\pydantic_ai_claim_triage.py `
  --database pydantic-ai-claim.db `
  --mode record `
  --live `
  --model $env:POLLARD_OPENAI_MODEL `
  --claim-id claim-1001
```

The successful live path makes two provider requests around one local claim
lookup. It disables SDK retries and OpenAI response storage, caps output at 512
tokens, disallows parallel tool calls, and applies pydantic-ai limits of two
requests and one tool call. Provider-side usage limits and the Pollard budget
are separate controls. The claim ID and retrieved claim record enter the
provider request and the local Pollard ledger. Keep this demo synthetic; do not
substitute real claim, policyholder, or customer data. The capability
normalizes each response and its usage into Pollard's result contract.

## pydantic-ai wrapper

The wrapper records one complete `Agent.run_sync()` call as one Pollard model
node. This is the simplest integration, but Pollard cannot see any internal
pydantic-ai tool calls added later. To govern those individually, wrap their
handlers or use a lower-level model boundary.

```powershell
python -m pip install -e ".[pydantic-ai]"
$env:OPENAI_API_KEY = "<project-api-key>"
python docs\recipes\pydantic_ai_wrap.py
```

The recipe supplies a caller-owned `AsyncOpenAI(max_retries=0)`, uses the
Responses model, caps output at 128 tokens, and disables OpenAI response
storage. The usage returned by pydantic-ai is normalized into Pollard's result
contract. See the current [pydantic-ai OpenAI model guide](https://pydantic.dev/docs/ai/models/openai/).

## MCP registry firewall

The local smoke test launches the repository's credential-free stdio server,
discovers its tools, builds a Pollard registry, and invokes `search` through
that registry:

```powershell
python -m pip install -e ".[mcp]"
'{"query":"pollard"}' | python docs\recipes\mcp_registry.py --stdio `
  --server-arg examples\mcp_demo_server.py `
  python search -
```

For Streamable HTTP, pass the server URL instead of `--stdio` and the command:

```powershell
'{"query":"pollard"}' | python docs\recipes\mcp_registry.py `
  https://mcp.example.test/mcp `
  search `
  -
```

The local example needs no network or credential. An HTTP server may require
headers, OAuth, TLS trust, or another client customization not exposed by this
small CLI. Construct that MCP session in application code and pass it to
`registry_from_mcp`. Treat tool annotations as descriptive metadata, not an
authorization decision. Review the MCP [transport specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and your server's authentication documentation.

## Inspect, replay, and clean up

Every successful script prints its root-specific inspection command. The
content-free default is safe for routine topology checks:

```powershell
pollard runs openai-tool-loop.db
pollard show openai-tool-loop.db <root-id>
pollard report openai-tool-loop.db <root-id> --json
pollard verify openai-tool-loop.db <root-id>
```

Use `--payloads` only where prompt, tool argument, and model result content is
allowed. Deleting a `.db` file removes the local recipe recording; it does not
delete provider-side logs or service-retained data.

Hybrid mode reuses only an exact existing semantic node. A new run root, model,
prompt, tool schema, provider marker, or attempt number can create a cache miss.
Use replay mode in application tests when a live request must be impossible.
Both LangChain workflow recipes and the pydantic-ai workflow recipe expose all
three modes directly. The Pydantic refund recipe performs its preview,
confirmed record, and strict replay sequence in one command.

The default-offline workflow recordings use `langchain-support-rag.db`,
`langchain-incident.db`, `pydantic-refund.db`, and `pydantic-ai-claim.db`.
Deleting one of these files removes only its local Pollard recording. It cannot
undo a confirmed side effect or delete a provider-side log.

## Common failures

| Symptom | Check |
|---|---|
| Authentication error | Required environment variable or cloud SDK identity; resource scope; token expiration; system clock |
| Model or deployment not found | Provider Region, endpoint, deployment name, model access, and exact pinned ID |
| Permission denied | Least-privilege inference permission and, when enabled, separate token-count permission |
| Budget refusal | Recorded estimated and settled charges; active run and window budgets; stale shared reservations |
| Missing recording in replay | Payload, parent, kind, attempt, and model must match the recorded identity exactly |
| Duplicate or surprising live charge | SDK retries, framework retries, a hybrid cache miss, or a framework-internal request outside Pollard |
| Offline workflow asks for a credential | Confirm that `--live` was not passed and that the selected mode and recipe are the intended ones |
| Generated Pydantic schema is refused | Use Pollard's supported schema subset; inspect unions, numeric types, formats, and constraint keywords |
| MCP tool refused | Tool was discovered, schema is supported, version is `mcp`, arguments validate, and policy permits it |
| Empty tool-call list | Model supports tool use, tool schema is valid, and the prompt makes the requested action clear |

When reporting a problem, include package versions, the redacted command, the
provider and model or deployment identifier, the Pollard root ID, and the
content-free `pollard show` and `pollard verify --json` output. Never include a
credential or a payload containing protected data.
