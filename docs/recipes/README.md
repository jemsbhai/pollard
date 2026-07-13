# Recipes

These scripts show Pollard inside existing agent stacks. Pollard never creates a
provider client or reads credentials. Each script expects the surrounding SDK to
use credentials configured by the user.

| Script | Install |
|---|---|
| `openai_tool_loop.py` | `pip install "pollard[openai]"` |
| `anthropic_tool_loop.py` | `pip install "pollard[anthropic]"` |
| `langgraph_node.py` | `pip install "pollard[openai]" langgraph` |
| `pydantic_ai_wrap.py` | `pip install pollard pydantic-ai` |
| `mcp_registry.py` | `pip install "pollard[mcp]"` |

The provider recipes make live calls and may incur provider charges. Review the
payload, model, and budget before running them. The checked-in examples disable
SDK retries and cap each provider response at 128 output tokens.

The MCP recipe accepts either a Streamable HTTP URL or a local stdio command.
The repository includes a credential-free local server for a complete smoke
test:

```powershell
python docs\recipes\mcp_registry.py --stdio `
  --server-arg examples\mcp_demo_server.py `
  python search '{\"query\":\"pollard\"}'
```

Phase 6 adds the `pollard show` command. Until then, each script prints the root
id that the command will inspect.
