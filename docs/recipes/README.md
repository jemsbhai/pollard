# Recipes

These scripts show Pollard inside existing agent stacks. Pollard never creates a
provider client or reads credentials. Each script expects the surrounding SDK to
use credentials configured by the user.

| Script | Install |
|---|---|
| `openai_tool_loop.py` | `pip install "pollard[openai]"` |
| `anthropic_tool_loop.py` | `pip install "pollard[anthropic]"` |
| `azure_openai.py` | `pip install "pollard[openai]"` |
| `bedrock_converse.py` | `pip install "pollard[bedrock]"` |
| `litellm_cloud.py` | `pip install "pollard[litellm]"` plus the selected provider dependency |
| `langgraph_node.py` | `pip install "pollard[openai]" langgraph` |
| `pydantic_ai_wrap.py` | `pip install pollard pydantic-ai` |
| `mcp_registry.py` | `pip install "pollard[mcp]"` |

The provider recipes make live calls and may incur provider charges; none runs
automatically. Review the payload, model, and budget before running one. The
checked-in examples disable SDK retries and cap each provider response at 128
output tokens. Hybrid mode can replay an identical stored call without billing,
but any payload or model change creates a different identity and can call the
provider.

OpenAI examples default to `gpt-5.6` and accept `POLLARD_OPENAI_MODEL` as an
override, following OpenAI's
[current model guide](https://developers.openai.com/api/docs/guides/latest-model).
They pass `store=False` to the Responses API. The Anthropic tool loop uses the
pinned `claude-sonnet-4-6` model by default and accepts
`POLLARD_ANTHROPIC_MODEL`; pin or override deliberately rather than assuming a
moving "latest" alias.

Azure OpenAI uses the existing OpenAI adapter because its current v1 endpoint
uses the standard OpenAI client contract. Bedrock has a direct Converse adapter.
The LiteLLM recipe covers Vertex AI, Azure AI, SageMaker, OCI, Watsonx,
Databricks, and the other routes supported by LiteLLM. See
[Cloud-hosted model providers](https://github.com/jemsbhai/pollard/blob/main/docs/cloud-providers.md)
for the support and credential matrix.

The MCP recipe accepts either a Streamable HTTP URL or a local stdio command.
The repository includes a credential-free local server for a complete smoke
test:

```powershell
python docs\recipes\mcp_registry.py --stdio `
  --server-arg examples\mcp_demo_server.py `
  python search '{\"query\":\"pollard\"}'
```

Every recipe writes a SQLite recording and prints the exact `pollard show`
command for inspecting it.
