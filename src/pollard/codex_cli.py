"""Run a recorded Codex task, or manage the caller's Codex login."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .adapters.codex import codex_environment, codex_executable, make_codex_exec_fn
from .errors import PollardError
from .governor import Budget
from .runtime import Runtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, prog="pollard-codex")
    parser.add_argument("--executable", help="Use a specific Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Run Codex's interactive login")
    commands.add_parser("status", help="Show Codex login status")
    commands.add_parser("version", help="Show the selected Codex CLI version")
    run = commands.add_parser("run", help="Execute one task or replay its recorded result")
    run.add_argument("--prompt", help="Task text; otherwise read UTF-8 text from stdin")
    run.add_argument("--cwd", type=Path, default=Path.cwd(), help="Target repository")
    run.add_argument(
        "--workspace-fingerprint",
        required=True,
        help="Identity for repository contents, instructions, and Codex configuration",
    )
    run.add_argument("--model", help="Explicit Codex model; otherwise uses Codex configuration")
    run.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    run.add_argument("--store", type=Path, default=Path("codex-runs.db"))
    run.add_argument("--run-label", required=True)
    run.add_argument("--mode", choices=("record", "hybrid", "replay"), default="record")
    run.add_argument("--attempt", type=int, default=0)
    run.add_argument("--steps", type=int, default=1, help="Pollard invocation budget")
    run.add_argument("--tokens", type=int, help="Account reported usage; not an in-task hard cap")
    run.add_argument("--timeout", type=float, default=300, help="Subprocess timeout in seconds")
    run.add_argument(
        "--max-output-bytes",
        type=int,
        default=8_388_608,
        help="Captured process output limit; not a model token cap",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "run":
            command = {"login": ["login"], "status": ["login", "status"], "version": ["--version"]}[
                args.command
            ]
            executable = codex_executable(args.executable)
            return subprocess.call(
                [str(executable), *command],
                env=codex_environment(executable=executable),
            )

        if args.attempt < 0 or args.steps < 0 or (args.tokens is not None and args.tokens < 0):
            parser.error("attempt, steps, and tokens must be nonnegative")
        if args.prompt is not None:
            prompt = args.prompt
        else:
            input_buffer = getattr(sys.stdin, "buffer", None)
            prompt = (
                input_buffer.read().decode("utf-8")
                if input_buffer is not None
                else sys.stdin.read()
            )
        if not prompt.strip():
            parser.error("provide a nonempty --prompt or prompt on stdin")
        if not args.workspace_fingerprint.strip():
            parser.error("workspace-fingerprint must not be empty")
        payload = {
            "prompt": prompt,
            "cwd": str(args.cwd.resolve()),
            "sandbox": args.sandbox,
            "workspace_fingerprint": args.workspace_fingerprint,
        }
        if args.model is not None:
            payload["model"] = args.model
        call = make_codex_exec_fn(
            executable=args.executable,
            timeout=args.timeout,
            max_output_bytes=args.max_output_bytes,
        )
        runtime = Runtime(args.store, mode=args.mode, refuse_duplicate_recordings=True)
        try:
            with runtime.run(
                args.run_label, budget=Budget(steps=args.steps, tokens=args.tokens)
            ) as run:
                node = run.model_call(payload, fn=call, attempt=args.attempt)
                print(
                    json.dumps(
                        {"node_id": node.id, "result": node.result, "report": run.report()},
                        ensure_ascii=True,
                    )
                )
        finally:
            close = getattr(runtime.store, "close", None)
            if callable(close):
                close()
        return 0
    except (ImportError, OSError, PollardError, RuntimeError, TypeError, ValueError) as error:
        # SDK/OS exceptions can contain credentials or process output. The
        # adapter's public errors carry safe actionable messages.
        print(f"pollard-codex: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
