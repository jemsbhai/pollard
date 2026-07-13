# EXP-006 end-to-end case studies

EXP-006 records all three Phase 9 candidate workloads. Each case uses Pollard's
OpenAI-compatible adapter against the same pinned local Qwen2.5-Coder 7B model,
a frozen action registry, branch and rollback, the core CLI, a committed
SQLite tree, a subtree seal, and a content-free HTML export. No hosted model
was called; provider spend was 0 USD.

## Cases

| Case | Workload | Rejected branch | Selected branch |
|---|---|---|---|
| EXP-006A | Research over three pinned local documents | Omits one document and fails coverage checks | Reads all documents and passes citation and unsupported-phrase checks |
| EXP-006B | Bug fix against a pinned repository and test suite | Leaves reversed bounds unhandled and fails one test | Handles reversed bounds and passes all four tests |
| EXP-006C | Household order through three local MCP stdio servers | Costs 2,897 cents against a 2,000-cent limit | Costs 1,547 cents and passes the budget policy |

The research case uses model-generated synthesis. The code-fix and household
cases use deterministic candidate controllers with model review; the evidence
does not claim that the model invented those candidate patches or orders.

Artifacts:

- [Combined manifest](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/manifest.json)
- [Research outcome](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/research/outcome.json)
- [Research seal](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/research/seal.json)
- [Research HTML tree](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/research/tree.html)
- [Code-fix outcome](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/code-fix/outcome.json)
- [Code-fix seal](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/code-fix/seal.json)
- [Code-fix HTML tree](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/code-fix/tree.html)
- [MCP household outcome](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/mcp-household/outcome.json)
- [MCP household seal](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/mcp-household/seal.json)
- [MCP household HTML tree](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-006/mcp-household/tree.html)

## Offline stranger verification

From a clone of the repository, Python 3.10 or newer is sufficient. The
verification path uses only the standard library and Pollard source in the
clone. It does not require the local model, llama.cpp, the MCP SDK, provider
credentials, or a network connection.

```powershell
$env:PYTHONPATH = (Resolve-Path src)
python examples\exp_006_verify.py
```

The verifier checks every manifest hash, every node and ancestor, each subtree
seal, each frozen registry digest, and the absence of remote HTML assets,
common credential patterns, and local user paths. It then replays both
root-to-leaf paths in each case under strict replay mode. Sentinel functions
fail the command if replay attempts to invoke a model or tool handler.

The expected summary is `ok: true`, six paths replayed across 49 nodes,
`network_used: false`, `model_calls_executed: 0`, and
`tool_calls_executed: 0`.

## Recording prerequisites

Re-recording is a separate, environment-specific operation. It requires the
pinned model and llama.cpp files named by hash in each outcome. EXP-006C also
requires `pollard[mcp]`. The three recording commands are:

```powershell
python examples\exp_006_research.py --server-binary <llama-server> --model <model>
python examples\exp_006_code_fix.py --server-binary <llama-server> --model <model>
python examples\exp_006_mcp_household.py --server-binary <llama-server> --model <model>
python examples\exp_006_verify.py --write-manifest
```

Re-recording may legitimately change results, timings, node IDs, seals, and
manifest hashes. Offline verification checks the committed recording; it does
not assert that stochastic inference will reproduce the same output.
