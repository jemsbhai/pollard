"""Record EXP-006B: fix a bug in a pinned repository with pinned tests."""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from _exp006_common import (
    finalize_case,
    local_llama_server,
    sha256_file,
    write_json,
)

from pollard import ActionSpec, Budget, Registry, Runtime, SQLiteStore
from pollard.adapters.openai import make_chat_completions_fn

WORKSPACES = ("baseline", "candidate-a", "candidate-b")
FILES = ("calculator.py", "test_calculator.py")

INCOMPLETE_FIX = '''"""Small pinned module with an intentional clamp bug for EXP-006B."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return value constrained to the inclusive lower/upper interval."""

    if value < lower:
        return lower
    if value > upper:
        return upper
    return value
'''

COMPLETE_FIX = '''"""Small pinned module fixed by the EXP-006B case study."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return value constrained to the inclusive lower/upper interval."""

    if lower > upper:
        raise ValueError("lower bound must not exceed upper bound")
    return max(lower, min(upper, value))
'''


def _text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise RuntimeError("local model result omitted text")
    return str(result["text"])


def _schemas() -> dict[str, dict[str, Any]]:
    workspace = {"type": "string", "enum": list(WORKSPACES)}
    file_name = {"type": "string", "enum": list(FILES)}
    return {
        "read": {
            "type": "object",
            "properties": {"workspace": workspace, "path": file_name},
            "required": ["workspace", "path"],
            "additionalProperties": False,
        },
        "write": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "enum": ["candidate-a", "candidate-b"],
                },
                "path": {"type": "string", "enum": ["calculator.py"]},
                "content": {"type": "string"},
            },
            "required": ["workspace", "path", "content"],
            "additionalProperties": False,
        },
        "workspace": {
            "type": "object",
            "properties": {"workspace": workspace},
            "required": ["workspace"],
            "additionalProperties": False,
        },
    }


def _registry(workspaces: dict[str, Path], temporary_root: Path) -> Registry:
    schemas = _schemas()

    def read_file(args: dict[str, Any]) -> dict[str, Any]:
        path = workspaces[str(args["workspace"])] / str(args["path"])
        content = path.read_text(encoding="utf-8")
        return {
            "path": str(args["path"]),
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def write_file(args: dict[str, Any]) -> dict[str, Any]:
        path = workspaces[str(args["workspace"])] / str(args["path"])
        content = str(args["content"])
        path.write_text(content, encoding="utf-8", newline="\n")
        return {
            "path": str(args["path"]),
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def run_tests(args: dict[str, Any]) -> dict[str, Any]:
        workspace = workspaces[str(args["workspace"])]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(workspace),
                "-p",
                "test_*.py",
                "-v",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        temporary = str(temporary_root)
        stdout = completed.stdout.replace(temporary, "<sandbox>")
        stderr = completed.stderr.replace(temporary, "<sandbox>")
        return {
            "passed": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def workspace_digest(args: dict[str, Any]) -> dict[str, Any]:
        workspace = workspaces[str(args["workspace"])]
        files = {name: sha256_file(workspace / name) for name in FILES}
        combined = hashlib.sha256(
            "".join(f"{name}:{digest}\n" for name, digest in sorted(files.items())).encode()
        ).hexdigest()
        return {"files": files, "digest": combined}

    return Registry(
        [
            ActionSpec(
                "read_file",
                "1",
                "Read one file from an isolated pinned workspace.",
                schemas["read"],
                False,
                read_file,
            ),
            ActionSpec(
                "write_file",
                "1",
                "Replace the implementation file in an isolated candidate workspace.",
                schemas["write"],
                True,
                write_file,
            ),
            ActionSpec(
                "run_tests",
                "1",
                "Run the pinned unittest suite in an isolated workspace.",
                schemas["workspace"],
                False,
                run_tests,
            ),
            ActionSpec(
                "workspace_digest",
                "1",
                "Hash every pinned workspace file after testing.",
                schemas["workspace"],
                False,
                workspace_digest,
            ),
        ]
    )


def _copy_repo(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in FILES:
        shutil.copyfile(source / name, destination / name)


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "run.db"
    db_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="pollard-exp-006b-") as temporary:
        temporary_root = Path(temporary)
        workspaces = {name: temporary_root / name for name in WORKSPACES}
        for workspace in workspaces.values():
            _copy_repo(args.repo, workspace)
        registry = _registry(workspaces, temporary_root)

        with local_llama_server(args.server_binary, args.model, port=args.port) as client:
            call_model = make_chat_completions_fn(
                client,
                max_tokens=384,
                seed=6006,
                temperature=0,
            )
            with SQLiteStore(db_path) as store:
                runtime = Runtime(store, registry=registry, mode="record")
                with runtime.run("exp-006b-pinned-code-fix", budget=Budget(steps=30)) as agent:
                    agent.note(
                        {
                            "case": "EXP-006B",
                            "adapter": "openai-compatible-chat",
                            "model": args.model_id,
                            "network": "loopback-only",
                        }
                    )
                    source = agent.tool_call(
                        "read_file",
                        {"workspace": "baseline", "path": "calculator.py"},
                        version="1",
                    )
                    tests = agent.tool_call(
                        "read_file",
                        {"workspace": "baseline", "path": "test_calculator.py"},
                        version="1",
                    )
                    baseline = agent.tool_call("run_tests", {"workspace": "baseline"}, version="1")
                    if baseline.result.get("passed"):
                        raise RuntimeError("pinned baseline unexpectedly passed")
                    diagnosis = agent.model_call(
                        {
                            "model": args.model_id,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "Diagnose only from supplied code and test output.",
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        f"Implementation:\n{source.result}\nTests:\n{tests.result}\n"
                                        f"Failure:\n{baseline.result}"
                                    ),
                                },
                            ],
                        },
                        fn=call_model,
                    )
                    branch_parent = agent.cursor_id

                    with agent.branch(attempt=0) as candidate_a:
                        candidate_a.model_call(
                            {
                                "model": args.model_id,
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": "Review a minimal bounds-only candidate fix.",
                                    },
                                    {
                                        "role": "user",
                                        "content": (
                                            f"Diagnosis:\n{_text(diagnosis.result)}\n"
                                            f"Candidate:\n{INCOMPLETE_FIX}"
                                        ),
                                    },
                                ],
                            },
                            fn=call_model,
                        )
                        candidate_a.tool_call(
                            "write_file",
                            {
                                "workspace": "candidate-a",
                                "path": "calculator.py",
                                "content": INCOMPLETE_FIX,
                            },
                            version="1",
                        )
                        bad_test = candidate_a.tool_call(
                            "run_tests", {"workspace": "candidate-a"}, version="1"
                        )
                        if bad_test.result.get("passed"):
                            raise RuntimeError("incomplete candidate unexpectedly passed")
                        candidate_a.note({"decision": "reject-incomplete-error-handling"})
                        candidate_a.prune()
                        rejected_tip = candidate_a.cursor_id

                    agent.rollback(branch_parent)
                    with agent.branch(attempt=1) as candidate_b:
                        candidate_b.model_call(
                            {
                                "model": args.model_id,
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "Review a complete fix against every pinned test."
                                        ),
                                    },
                                    {
                                        "role": "user",
                                        "content": (
                                            f"Prior failure:\n{bad_test.result}\n"
                                            f"Candidate:\n{COMPLETE_FIX}"
                                        ),
                                    },
                                ],
                            },
                            fn=call_model,
                        )
                        write_result = candidate_b.tool_call(
                            "write_file",
                            {
                                "workspace": "candidate-b",
                                "path": "calculator.py",
                                "content": COMPLETE_FIX,
                            },
                            version="1",
                        )
                        good_test = candidate_b.tool_call(
                            "run_tests", {"workspace": "candidate-b"}, version="1"
                        )
                        if not good_test.result.get("passed"):
                            raise RuntimeError("complete candidate did not pass pinned tests")
                        digest = candidate_b.tool_call(
                            "workspace_digest",
                            {"workspace": "candidate-b"},
                            version="1",
                        )
                        candidate_b.note({"decision": "select-all-pinned-tests-pass"})
                        selected_tip = candidate_b.cursor_id
                    root_id = agent.root_id
                    report = agent.report()

        fixed_dir = output_dir / "fixed"
        fixed_dir.mkdir(exist_ok=True)
        shutil.copyfile(workspaces["candidate-b"] / "calculator.py", fixed_dir / "calculator.py")
        write_json(output_dir / "test-result.json", good_test.result)

    artifact = finalize_case(db_path, root_id, output_dir)
    outcome = {
        "id": "EXP-006B",
        "status": "passed",
        "workload": "code-fix-pinned-repository-and-tests",
        "adapter": "pollard.adapters.openai.make_chat_completions_fn",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "model": {
            "id": args.model_id,
            "sha256": sha256_file(args.model),
            "llama_cpp_release": args.llama_release,
            "server_sha256": sha256_file(args.server_binary),
        },
        "inputs": {name: sha256_file(args.repo / name) for name in FILES},
        "registry_digest": registry.registry_digest,
        "baseline_passed": False,
        "rejected_test": bad_test.result,
        "rejected_tip": rejected_tip,
        "selected_test": good_test.result,
        "selected_write": write_result.result,
        "selected_workspace": digest.result,
        "selected_tip": selected_tip,
        "fixed_file_sha256": sha256_file(output_dir / "fixed" / "calculator.py"),
        "report": report,
        "artifact": artifact,
        "provider_spend_usd": 0,
    }
    write_json(output_dir / "outcome.json", outcome)
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", default="qwen2.5-coder:7b")
    parser.add_argument("--llama-release", default="b9630")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("evidence/EXP-006/inputs/code-fix/repo"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/EXP-006/code-fix"),
    )
    parser.add_argument("--port", type=int, default=8132)
    args = parser.parse_args()
    outcome = run(args)
    print(outcome["artifact"]["seal_digest"])


if __name__ == "__main__":
    main()
