# Codex CLI integration

Python Pollard 1.6.2 can run Codex CLI tasks as recorded, governed steps.
The optional `codex` extra installs OpenAI's native CLI runtime, pinned to
0.162.1. Pollard's core still has no runtime dependencies. The native npm and
Rust packages have independent versions and do not include this adapter.

## Install and authenticate

```powershell
python -m pip install "pollard[codex]==1.6.2"
pollard-codex version
pollard-codex login
pollard-codex status
```

`login` opens Codex's normal interactive authentication flow. `status` reports
the selected runtime's login status. Both use the caller's Codex configuration
and account; Pollard does not create an OpenAI client or store login credentials.
Existing Codex login state is reused when the same `CODEX_HOME` is used.
Keep credentials out of prompts, fingerprints, run labels, and recordings.

The extra provides native wheels for Windows x64/ARM64, macOS x64/ARM64,
and Linux x64/ARM64, including glibc and musl. It downloads a large optional
binary package. It does not install a command named `codex`; Pollard supplies
the `pollard-codex` entry point and resolves the bundled executable directly.

With an existing CLI, `pip install pollard` is enough. The adapter prefers the
bundled runtime when installed, then searches `PATH`. Choose a specific native
binary with `pollard-codex --executable /path/to/codex version` or the adapter's
`executable=` argument. Windows `.cmd` and `.bat` launchers are rejected; use
the extra or a native `codex.exe`. Other CLI versions must support the same
flags and JSONL contract; the pinned version is the tested compatibility target.

## Run and record a task

Run these commands from the repository being reviewed:

```powershell
pollard-codex run --prompt "Review this repository and summarize findings without editing files." --cwd . --workspace-fingerprint "review-snapshot-001-config-001" --run-label review-001 --store codex-runs.db
```

This is a live Codex task and may consume account usage or incur API charges.
The command defaults to a read-only sandbox, one Pollard step, a 300-second
subprocess timeout, and an 8 MiB cap on each captured output stream. Use
`--sandbox workspace-write` only when the task should edit the target workspace.
Use `--model` to select an available model explicitly; otherwise Codex chooses
its configured model. Prompts may also arrive through stdin when `--prompt`
is omitted. `--help` and `version` make no model request.

The result is JSON containing `node_id`, `result`, and `report`. The result
contains final answer text, the Codex thread ID, and compatible reported usage.
Full reasoning, command output, raw event history, and stderr are not retained.
Prompts and final answers are retained in the Pollard store and may contain
sensitive repository information; normal Pollard data governance applies.

One invocation is one fresh, ephemeral Codex task. The adapter does not resume
a Codex conversation. `--ephemeral` disables normal Codex session persistence;
it does not disable the Pollard recording or undo tool side effects.

## Replay identity and retries

`workspace_fingerprint` is a required caller declaration, not an automatic scan.
Use a digest or controlled snapshot ID covering all relevant inputs: tracked
and untracked file contents, repository instructions, effective Codex model and
configuration, MCP tool definitions, runtime version, and relevant environment.
A Git commit alone is sufficient only when it captures all relevant inputs and
the remaining context is fixed. Never put raw secrets in this field.

The prompt, absolute working directory, sandbox, supplied model, and fingerprint
are recorded in the step identity. Factory options such as environment overrides
are not automatically fingerprinted. Update the fingerprint when they matter.

Repeat the same arguments with `--mode replay` to return the stored result:

```powershell
pollard-codex run --prompt "Review this repository and summarize findings without editing files." --cwd . --workspace-fingerprint "review-snapshot-001-config-001" --run-label review-001 --store codex-runs.db --mode replay
```

Strict replay never resolves or launches Codex and needs no extra or login.
It fails on a missing recording. Hybrid mode reuses an exact recording and
executes a live task on a miss. Replay returns the recorded answer; it does not
reapply a patch, reconstruct a workspace, restore a Codex session, or prove that
the current repository still matches the supplied fingerprint.

The CLI enables `refuse_duplicate_recordings=True`. Repeating the same record
identity is refused before launch. Use `--attempt 1 --steps 2` or a new run label
only for an intentional new execution. Increasing the attempt also requires
enough remaining run budget. This guard is not a distributed exactly-once lock.
The adapter itself never retries. Codex's internal model/transport retry behavior
remains governed by Codex configuration.

## Python adapter

```python
from pathlib import Path
from pollard import Budget, Runtime
from pollard.adapters.codex import make_codex_exec_fn

call = make_codex_exec_fn(timeout=300, max_output_bytes=8_388_608)
payload = {
    "prompt": "Review this repository without editing files.",
    "cwd": str(Path.cwd().resolve()),
    "sandbox": "read-only",
    "workspace_fingerprint": "review-snapshot-001-config-001",
}
with Runtime("codex-runs.db", refuse_duplicate_recordings=True).run(
    "python-review-001", budget=Budget(steps=1)
) as run:
    node = run.model_call(payload, fn=call)
    print(node.result["text"])
    print(run.report())
```

`make_codex_exec_fn(*, executable=None, timeout=300,
max_output_bytes=8_388_608, env=None)` returns a synchronous step function.
`env` supplies process environment overrides without adding their values to
the recording. Resolution is deferred until the callable is invoked.

Required payload fields are nonempty `prompt`, an absolute existing `cwd`,
and nonempty `workspace_fingerprint`. Optional fields are `model` and `sandbox`
(`read-only` or `workspace-write`). Reserved `_pollard` metadata is ignored by
Codex. Other fields fail before launch. Explicit `sandbox` and `model` fields
make the recorded intent clearer than relying on defaults.

For registry policy checks, register a handler which builds this payload from
validated arguments, then call `run.tool_call` through that registry. A bare
`model_call` provides metering and recording, not registry tool authorization.

`codex_executable(executable=None)` resolves the selected native binary without
starting it. `codex_environment(env=None, *, executable=None)` builds its child
environment and includes the bundled helper directory when needed.
`CodexExecError` exposes a safe message, `code`, and optional `returncode`.

## Accounting and failure boundaries

- Each task counts as one Pollard model step. Internal Codex model requests,
  shell commands, file edits, and MCP calls are not separate governed nodes.
- Successful completion requires a zero process exit, a valid JSONL sequence,
  a completed turn, and final answer text. Failed, incomplete, interrupted, or
  malformed output is not stored as a successful answer.
- Valid `input_tokens` and `output_tokens` are normalized into `usage`.
  `provider_usage` preserves cache/reasoning details. Input already includes
  cached input, and output already includes reasoning; these must not be added
  again. Missing or invalid usage remains unknown and triggers the usual meter
  warning, rather than inventing zero provider usage.
- Token and dollar accounting is retrospective at this boundary. `--tokens`
  limits subsequent Pollard steps based on reported usage; it cannot impose a
  hard token ceiling inside a running Codex task. `max_output_bytes` limits
  captured process output, not model generation or provider billing.
  Codex reports aggregate task usage; per-request context limits and tiered
  provider pricing must not treat that aggregate as one API request. Exact
  billing remains the provider's responsibility.
- The adapter starts an independent process with `--no-daemon`, sends the
  prompt through stdin without a shell, and retains Codex sandbox controls.
  It inherits user/project configuration, instructions, hooks, environment,
  and configured MCP access. Read-only filesystem access is not a guarantee
  that connected external tools have no side effects.
- Timeout, output overflow, interruption, and post-launch failures trigger
  process-tree cleanup and are marked as an unknown external outcome. Detached
  descendants or already-completed external actions may survive. Inspect the
  workspace and external system before retrying. Pollard rollback does not undo
  edits. Cleanup can add a bounded termination wait after the requested timeout.
  Codex sandbox and operating-system isolation remain relevant controls.

## Offline verification

```powershell
python examples/19_codex_recording.py
python -m pytest tests/test_codex_adapter.py tests/test_codex_cli.py
```

The example uses a clearly labeled synthetic callable, launches Codex zero
times, and records/replays 20 synthetic tokens. Adapter tests exercise the
subprocess protocol with local fixtures, including refusals and failure cleanup.
The optional installed-runtime smoke check runs only `--version` and needs no
account. CI makes no model request.

See the official
[Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
for the JSONL contract and authentication, and the
[Codex SDK documentation](https://learn.chatgpt.com/docs/codex-sdk) for the
official SDK and its pinned native runtime. Pollard currently supplies a CLI
task adapter, not an SDK-thread, lifecycle-hook, or MCP gateway integration.
