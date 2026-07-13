# Cloud-hosted model providers

Pollard governs a call at the client boundary. It does not require the model to
be hosted by the company that created it, and it does not read cloud
credentials. Your application constructs the provider client, then passes that
client or callable to a Pollard adapter.

## Choose an integration boundary

Prefer a direct adapter when the provider has one and the application needs its
native request fields, streaming events, token usage, or tool-call structure.
Use LiteLLM when one normalized chat-completion surface across several clouds is
more important than provider-native features. Use the generic step-function
contract when an internal gateway or SDK cannot expose either interface.

The boundary is semantic, not transport-specific. Pollard records the payload
given to the step and the normalized result returned by the callable. It does
not record HTTP headers unless an application incorrectly puts them in that
payload. Authentication should remain inside the caller-owned SDK client.

## Supported paths

| Hosting path | Pollard integration | Install | Caller-owned configuration |
|---|---|---|---|
| OpenAI API | Responses or Chat Completions adapter | `pollard[openai]` | OpenAI project key, model, optional organization or project |
| Anthropic API | Messages and Messages streaming adapter | `pollard[anthropic]` | Anthropic workspace key and model |
| Azure OpenAI v1 endpoint | OpenAI adapter with an Azure-configured `OpenAI` client | `pollard[openai]` for API key or `pollard[azure-openai]` for Entra ID | Endpoint, deployment name, API key or Entra token provider |
| Amazon Bedrock Converse | Direct Converse and ConverseStream adapter | `pollard[bedrock]` | AWS SDK identity, Region, model or inference profile, model access, IAM |
| Vertex AI | LiteLLM completion adapter | `pollard[litellm]` plus Google route dependencies | Application Default Credentials, project, location, model |
| Microsoft Foundry Models and Azure AI inference | LiteLLM completion adapter | `pollard[litellm]` plus Azure route dependencies | Endpoint, deployment or model, key or Entra identity as supported by the route |
| SageMaker, OCI, Watsonx, Databricks, and other LiteLLM routes | LiteLLM completion adapter | `pollard[litellm]` plus route dependencies | Provider-specific endpoint, identity, model, and Region or project |
| Any OpenAI-compatible gateway | OpenAI Chat Completions adapter or LiteLLM adapter | `pollard[openai]` or `pollard[litellm]` | Gateway base URL, gateway key when required, model route |

The direct adapters preserve provider-native request fields. LiteLLM is the
portability path when one normalized chat interface is more useful than direct
access to a cloud API.

## OpenAI API

The direct OpenAI adapter supports Responses, Responses streaming, Chat
Completions, and Chat Completions streaming. A caller supplies a configured
sync or async OpenAI client. The first-party recipe uses Responses, disables
SDK retries, caps output at 128 tokens, and passes `store=False`.

```python
from openai import OpenAI
from pollard import Runtime
from pollard.adapters.openai import make_responses_fn

client = OpenAI(max_retries=0)
call_model = make_responses_fn(client, store=False)

with Runtime("openai.db").run("openai") as run:
    node = run.model_call(
        {
            "_pollard": {"provider": "openai"},
            "model": "gpt-5.6",
            "input": "Explain content addressing in two sentences.",
            "max_output_tokens": 128,
            "reasoning": {"effort": "none"},
        },
        fn=call_model,
    )
```

The SDK reads `OPENAI_API_KEY` by default. An account credit balance is not a
pre-execution dollar budget. Configure provider-side project limits, disable
unwanted retries, and keep Pollard token limits in addition to that account
control. Review OpenAI's
[model guidance](https://developers.openai.com/api/docs/guides/latest-model)
before changing the recipe default.

## Anthropic API

The direct Anthropic adapter supports Messages and Messages streaming. Its sync
callable also implements Pollard's input-token estimator by calling Anthropic's
token-count endpoint when installed in a `TokenMeter`.

```python
from anthropic import Anthropic
from pollard import Runtime
from pollard.adapters.anthropic import make_messages_fn

client = Anthropic(max_retries=0)
call_model = make_messages_fn(client, max_tokens=128)

with Runtime("anthropic.db").run("anthropic") as run:
    node = run.model_call(
        {
            "_pollard": {"provider": "anthropic"},
            "model": "claude-sonnet-5",
            "messages": [
                {"role": "user", "content": "Explain content addressing."}
            ],
            "output_config": {"effort": "low"},
        },
        fn=call_model,
    )
```

The SDK reads `ANTHROPIC_API_KEY` by default. Model IDs from the 4.6 generation
onward are pinned snapshots even when they do not contain a date. Check the
official [Claude model table](https://platform.claude.com/docs/en/about-claude/models/overview)
for current availability, limits, and prices.

## Azure OpenAI

Microsoft's Azure OpenAI v1 guidance configures the standard OpenAI client with
an Azure base URL. The deployment name goes in the `model` field. Pollard's
Responses and Chat Completions adapters work with that client because the
request and response contracts are OpenAI contracts.

```python
import os

from openai import OpenAI
from pollard import Runtime
from pollard.adapters.openai import make_responses_fn

client = OpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    base_url=f"{os.environ['AZURE_OPENAI_ENDPOINT'].rstrip('/')}/openai/v1/",
    max_retries=0,
)
call_model = make_responses_fn(client, store=False)

with Runtime("azure-openai.db").run("azure-openai") as run:
    node = run.model_call(
        {
            "_pollard": {"provider": "azure.ai.openai"},
            "model": os.environ["AZURE_OPENAI_DEPLOYMENT"],
            "input": "Explain content addressing in two sentences.",
            "max_output_tokens": 128,
        },
        fn=call_model,
    )
```

The reserved `_pollard` object is committed into node identity but removed by
Pollard adapters before the provider request. It gives exports a place for
provider metadata without sending an unsupported SDK argument.

For Microsoft Entra ID, construct the same client with a token provider instead
of an API key; the checked-in recipe exposes this as `--entra-id` and uses
`DefaultAzureCredential`. Authentication remains outside Pollard. See Microsoft's
[endpoint and authentication guide](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/switching-endpoints).

OpenAI Responses are stored by the service by default. The example passes
`store=False` so the provider does not retain the response through that API
feature; this is separate from Pollard's local recording and from any provider
abuse-monitoring or legal-retention obligations. See OpenAI's
[Responses migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses).

## Amazon Bedrock

The Bedrock adapter targets the provider-neutral Converse API. It normalizes
text, tool use, streaming deltas, and `inputTokens`/`outputTokens` into the
Pollard result contract while retaining the original Bedrock response fields.

```python
import boto3

from pollard import Runtime
from pollard.adapters.bedrock import make_converse_fn

client = boto3.client("bedrock-runtime", region_name="us-east-1")
call_model = make_converse_fn(client)

with Runtime("bedrock.db").run("bedrock") as run:
    node = run.model_call(
        {
            "_pollard": {"provider": "aws.bedrock"},
            "modelId": "us.amazon.nova-lite-v1:0",
            "messages": [
                {"role": "user", "content": [{"text": "Say hello in five words."}]}
            ],
            "inferenceConfig": {"maxTokens": 128},
        },
        fn=call_model,
    )
```

Set `stream=True` on `make_converse_fn` to use `converse_stream`. Set
`count_tokens=True` to opt into Bedrock's separate CountTokens request during a
token-meter precheck. AWS does not charge for CountTokens, but some models and
cross-Region inference profiles do not support it. CountTokens needs the
`bedrock:CountTokens` permission. Converse needs `bedrock:InvokeModel`, and
ConverseStream needs `bedrock:InvokeModelWithResponseStream`.

Use a model ID or inference profile available in the selected Region. Model
access and Region availability are separate from IAM. See the
[Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html),
[CountTokens API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CountTokens.html),
and [model-access guide](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

AWS credentials can come from environment variables, shared configuration,
IAM Identity Center, container credentials, or an instance role through the
standard [AWS SDK credential chain](https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html).
Do not pass access keys as script arguments or Pollard payload fields. Prefer a
role limited to the exact model resources and operations the application uses.

## LiteLLM and other clouds

Pollard's LiteLLM adapter accepts the same provider-prefixed model routes as
LiteLLM. Examples include `bedrock/...`, `azure_ai/...`, and `vertex_ai/...`.

```python
from litellm import completion
from pollard import Runtime
from pollard.adapters.litellm import make_completion_fn

call_model = make_completion_fn(completion, num_retries=0, max_tokens=128)
with Runtime("cloud-gateway.db").run("cloud-gateway") as run:
    node = run.model_call(
        {
            "_pollard": {"provider": "gcp.vertex_ai"},
            "model": "vertex_ai/gemini-2.5-flash",
            "messages": [{"role": "user", "content": "Say hello in five words."}],
        },
        fn=call_model,
    )
```

Provider credentials and provider-specific packages are configured according
to [LiteLLM's provider documentation](https://docs.litellm.ai/docs/providers).
Pollard neither copies those credentials into a node nor adds retries.

The checked-in `litellm_cloud.py` recipe uses the same code with different
routes:

```powershell
# Microsoft Foundry Models or another Azure AI inference endpoint
python docs\recipes\litellm_cloud.py azure_ai/<deployment-or-model> `
  --provider azure.ai.inference

# Google Vertex AI
python docs\recipes\litellm_cloud.py vertex_ai/gemini-2.5-flash `
  --provider gcp.vertex_ai

# Amazon Bedrock through LiteLLM instead of the direct Converse adapter
python docs\recipes\litellm_cloud.py bedrock/us.amazon.nova-lite-v1:0 `
  --provider aws.bedrock
```

Azure AI routes use the settings documented on LiteLLM's
[Azure AI provider page](https://docs.litellm.ai/docs/providers/azure_ai).
Vertex AI and Bedrock use their normal cloud SDK credential chains.

For Vertex AI, Application Default Credentials can come from a local developer
login, an attached service account, or workload identity. The project and
location remain provider route configuration. Follow Google's
[Application Default Credentials guide](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc)
and avoid long-lived service-account keys where workload identity is available.

Microsoft Foundry Models and Azure AI inference endpoints are distinct from an
Azure OpenAI resource even though both are Azure services. Use the OpenAI
adapter only when the endpoint presents the Azure OpenAI v1 contract. Use the
documented LiteLLM `azure_ai/...` route or a caller-owned generic function for
other Foundry model endpoints. For keyless production access, consult
Microsoft's [Entra ID configuration guide](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-entra-id).

## OpenAI-compatible gateways

For an internal or third-party OpenAI-compatible Chat Completions endpoint,
configure the caller-owned `OpenAI` client with its base URL and key, then use
`make_chat_completions_fn`. Compatibility is a claim by the gateway, not by
Pollard. Verify tool schemas, usage fields, streaming chunks, retry behavior,
and the provider's meaning of `model` against a frozen fixture before using it
for production replay.

```python
import os

from openai import OpenAI
from pollard.adapters.openai import make_chat_completions_fn

client = OpenAI(
    base_url="https://gateway.example.test/v1",
    api_key=os.environ["GATEWAY_API_KEY"],
    max_retries=0,
)
call_model = make_chat_completions_fn(client, max_tokens=128)
```

## Credential boundary

Pollard needs no cloud credential of its own. Depending on the chosen path, the
surrounding SDK may need:

- OpenAI API: a project API key, model access, and any selected organization or
  project scope.
- Anthropic API: a workspace API key and access to the pinned model.
- Azure OpenAI: an endpoint, deployment name, and either an API key or a
  Microsoft Entra ID token provider.
- Amazon Bedrock: a normal AWS credential chain, a Region, model access, and IAM
  permissions for inference. CountTokens permission is needed only when its
  precheck is enabled.
- Vertex AI: Google Application Default Credentials plus project and location
  configuration, when required by the LiteLLM route.
- Microsoft Foundry Models: the inference endpoint, deployment or model name,
  and an API key or supported Entra identity.
- Other LiteLLM providers: the variables or client options named by that
  provider's LiteLLM page.

Do not place secrets in a model-call payload. SDK clients own authentication;
Pollard stores the payload as part of the audit ledger.

## Operational checklist

- Pin a provider model or deployment identifier and record the hosting Region
  or project outside secret-bearing payload fields.
- Disable or explicitly budget SDK, gateway, and framework retries.
- Set provider-side dollar or quota controls; Pollard's token, request, and step
  limits are complementary controls.
- Decide whether provider response storage, abuse monitoring, and legal
  retention are acceptable independently of the Pollard store.
- Confirm normalized `text`, `tool_calls`, and `usage` with a frozen response
  fixture before a live request.
- Use `mode="replay"` in CI. Do not make provider credentials available to
  offline tests.
- Run `pollard verify`, save a subtree seal when exporting evidence, and keep
  secrets out of payloads, results, metadata, run labels, and error reports.
