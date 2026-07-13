# Cloud-hosted model providers

Pollard governs a call at the client boundary. It does not require the model to
be hosted by the company that created it, and it does not read cloud
credentials. Your application constructs the provider client, then passes that
client or callable to a Pollard adapter.

## Supported paths

| Hosting path | Pollard integration | Install |
|---|---|---|
| OpenAI API | Responses or Chat Completions adapter | `pollard[openai]` |
| Azure OpenAI v1 endpoint | The same OpenAI adapter, with an Azure-configured `OpenAI` client | `pollard[openai]` |
| Amazon Bedrock Converse | Direct Converse and ConverseStream adapter | `pollard[bedrock]` |
| Anthropic API | Messages adapter | `pollard[anthropic]` |
| Vertex AI, Azure AI, SageMaker, OCI, Watsonx, Databricks, and other LiteLLM routes | LiteLLM completion adapter | `pollard[litellm]` plus the provider dependency LiteLLM requires |
| Any OpenAI-compatible gateway | OpenAI Chat Completions adapter, or the LiteLLM adapter | `pollard[openai]` or `pollard[litellm]` |

The direct adapters preserve provider-native request fields. LiteLLM is the
portability path when one normalized chat interface is more useful than direct
access to a cloud API.

## Azure OpenAI

Microsoft's current Azure OpenAI v1 guidance configures the standard OpenAI
client with an Azure base URL. The deployment name goes in the `model` field.
Pollard's existing Responses and Chat Completions adapters work with that
client because the request and response contracts are the OpenAI contracts.

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
of an API key. Authentication remains outside Pollard. See Microsoft's
[Azure OpenAI Responses migration guide](https://learn.microsoft.com/en-us/azure/developer/ai/how-to/azure-openai-to-responses).

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
`bedrock:CountTokens` permission; model inference needs the applicable
`bedrock:InvokeModel` permissions. Use a model ID or inference profile available
in the selected Region. See the
[Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
and [CountTokens API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CountTokens.html).

## LiteLLM and other clouds

Pollard's LiteLLM adapter accepts the same provider-prefixed model routes as
LiteLLM. Examples include `bedrock/...`, `azure/...`, and `vertex_ai/...`.

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

Provider credentials and any provider-specific package are configured according
to [LiteLLM's provider documentation](https://docs.litellm.ai/docs/providers).
Pollard neither copies those credentials into a node nor adds retries.

The checked-in `litellm_cloud.py` recipe can use the same code with different
cloud routes:

```powershell
# Microsoft Foundry Models or another Azure AI inference endpoint
python docs\recipes\litellm_cloud.py azure_ai/command-r-plus `
  --provider azure.ai.inference

# Google Vertex AI
python docs\recipes\litellm_cloud.py vertex_ai/gemini-2.5-flash `
  --provider gcp.vertex_ai

# Amazon Bedrock through LiteLLM instead of the direct Converse adapter
python docs\recipes\litellm_cloud.py bedrock/us.amazon.nova-lite-v1:0 `
  --provider aws.bedrock
```

Azure AI routes use the `AZURE_AI_API_KEY` and `AZURE_AI_API_BASE` settings
documented on LiteLLM's
[Azure AI provider page](https://docs.litellm.ai/docs/providers/azure_ai).
Vertex AI and Bedrock use their normal cloud SDK credential chains.

## Credential boundary

Pollard needs no cloud credential of its own. Depending on the chosen path, the
surrounding SDK may need:

- Azure OpenAI: an endpoint, deployment name, and either an API key or a
  Microsoft Entra ID token provider.
- Amazon Bedrock: a normal AWS credential chain, a Region, model access, and IAM
  permissions for inference. CountTokens permission is needed only when its
  precheck is enabled.
- Vertex AI: Google Application Default Credentials plus project and location
  configuration, when required by the LiteLLM route.
- Other LiteLLM providers: the variables or client options named by that
  provider's LiteLLM page.

Do not place secrets in a model-call payload. SDK clients own authentication;
Pollard stores the payload as part of the audit ledger.
