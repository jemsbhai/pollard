# Pollard MCP server

The `pollard-mcp` command exposes governed upstream tool calls and stored-run
inspection to an MCP host over stdio. It is available from the checkout and is
not included in the published Python 1.6.2 wheel. Install the checkout with its
optional MCP dependency:

```sh
python -m pip install -e ".[mcp]"
pollard-mcp --self-test
```

Configure the host to launch `pollard-mcp` from that environment. An absolute
path to the executable avoids selecting a different Python installation.
`python -m pollard.mcp_server` is equivalent. The server starts only when
launched; importing `pollard` or using its existing CLI does not start it or
require the MCP SDK. `--help` and `--self-test` exit without opening a transport.
The self-test validates the five registered tools and prints their names.

## Tools

| Tool | Behavior |
| --- | --- |
| `governed_mcp_call` | Discover an upstream registry, validate one tool call, enforce its step budget, and record the result in SQLite. |
| `list_runs` | List the roots in an existing store. |
| `show_run` | Render a stored tree by its root ID. |
| `run_report` | Report spent and avoided charges for a root. |
| `verify_store` | Verify stored identities and results. |

`governed_mcp_call` accepts a streamable HTTP URL in `upstream`. To launch a
local upstream process, set `stdio` to `true`, put the executable in `upstream`,
and pass its arguments in `server_args`. For example, with absolute paths to
the Python interpreter and this repository's credential-free demo server:

```json
{
  "upstream": "/path/to/venv/bin/python",
  "stdio": true,
  "server_args": ["/path/to/pollard/examples/mcp_demo_server.py"],
  "tool": "search",
  "arguments": {"query": "pollard"},
  "budget_steps": 1,
  "database": "/path/to/recordings.db"
}
```

The returned JSON contains `result`, `root_id`, and an inspection command.
Every invocation creates a fresh root and makes a live call, even if the
upstream, tool, and arguments match a previous request. Separate requests have
separate budgets. There is no implicit replay or retry of a side effect.
Existing recordings remain inspectable; the server does not reuse roots from
the original implementation's shared `mcp-registry` run.

The default budget is three steps, and this tool dispatches at most one action
per invocation. A zero budget refuses that action; negative budgets are
rejected before connecting. A step budget does not cap the upstream tool's
internal work, duration, tokens, or monetary cost. Store connections close on
success, refusal, cancellation, and errors. Inspection uses the server's own
Python installation with isolated stdin, treats database paths and root IDs as
positional values, and reports CLI failures as MCP tool errors. Inspection
returns JSON with complete root IDs and runs without blocking the MCP event
loop.

## Host permissions

Use this server with a trusted local MCP host. Its tools can launch the
specified local program, contact the specified upstream URL, and access store
paths with the server process's operating-system permissions. Pollard's
registry and budget checks govern the selected upstream action; they do not
sandbox process startup, restrict network destinations, or authorize filesystem
access. Configure those restrictions in the host or operating system when
needed. Upstream application errors remain in the recorded MCP result.

The server runs over stdio and does not expose a listening HTTP endpoint.
The test suite exercises the real stdio handshake, upstream dispatch, budget
refusal, repeated calls, Unicode results, and all four inspection tools without
provider credentials or remote services.
