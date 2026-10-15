import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pollard import Budget, MemoryStore, Runtime
from pollard.adapters import codex
from pollard.adapters.codex import CodexExecError, make_codex_exec_fn
from pollard.errors import is_post_dispatch_outcome_unknown
from pollard.meters import StepMeter, TokenMeter

USAGE = {
    "input_tokens": 11,
    "cached_input_tokens": 7,
    "cache_write_input_tokens": 0,
    "output_tokens": 4,
    "reasoning_output_tokens": 2,
}
EVENTS: list[dict[str, Any]] = [
    {"type": "thread.started", "thread_id": "thread-fixture"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "working"}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
    {
        "type": "item.completed",
        "item": {"type": "command_execution", "aggregated_output": "private tool output"},
    },
    {"type": "item.completed", "item": {"type": "agent_message", "text": "hello ✓"}},
    {"type": "turn.completed", "usage": USAGE},
]


def protocol(events: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")


def output_program(output: bytes, *, stderr: bytes = b"", status: int = 0) -> str:
    return (
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write({output!r})\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        f"sys.exit({status})\n"
    )


@pytest.fixture
def payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "prompt": "Review this repository",
        "cwd": str(tmp_path),
        "workspace_fingerprint": "snapshot-and-config-v1",
    }


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run real pipes/processes; substitute only the native Codex executable.

    The shim invokes Python directly on every platform, never a shell or paid
    provider. Other Popen calls (including Windows taskkill) stay native.
    """

    native_popen = subprocess.Popen
    binary = str(Path(sys.executable).resolve())
    calls: list[tuple[list[str], dict[str, Any]]] = []
    processes: list[subprocess.Popen[bytes]] = []

    def setup(program: str, **options: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def spawn(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            if args[0] == binary:
                calls.append((list(args), dict(kwargs)))
                process = native_popen([binary, "-c", program, *args[1:]], **kwargs)
                processes.append(process)
                return process
            return native_popen(args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", spawn)
        return make_codex_exec_fn(executable=binary, **options)

    return setup, calls, processes


def test_real_process_normalizes_only_final_answer_and_usage(fake_cli: Any, payload: Any) -> None:
    setup, calls, processes = fake_cli
    fn = setup(output_program(protocol(EVENTS), stderr=b"private stderr"))
    result = fn(payload)
    assert result == {
        "text": "hello ✓",
        "thread_id": "thread-fixture",
        "usage": {"input_tokens": 11, "output_tokens": 4},
        "provider_usage": USAGE,
    }
    assert "private" not in repr(result)
    assert len(calls) == 1
    assert processes[0].poll() == 0


def test_command_stdin_environment_and_defaults(fake_cli: Any, payload: Any) -> None:
    setup, calls, _processes = fake_cli
    payload.update({"prompt": "Unicode ✓\n$(never executed) & 'quoted'", "_pollard": {"id": 7}})
    program = (
        "import json,sys,os\n"
        "text=json.dumps({'prompt':sys.stdin.buffer.read().decode('utf-8'),"
        "'cwd':os.getcwd(),'env':os.environ['POLLARD_CODEX_TEST']})\n"
        f"events={EVENTS!r}\n"
        "events[-2]['item']['text']=text\n"
        "for event in events: print(json.dumps(event),flush=True)\n"
    )
    env = {"POLLARD_CODEX_TEST": "inherited override"}
    fn = setup(program, env=env)
    env["POLLARD_CODEX_TEST"] = "mutated after construction"
    result = fn(payload)
    assert json.loads(result["text"]) == {
        "prompt": payload["prompt"],
        "cwd": payload["cwd"],
        "env": "inherited override",
    }
    args, options = calls[0]
    assert args[1:] == [
        "--no-daemon", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
        "-C", payload["cwd"], "-",
    ]
    assert options["shell"] is False
    assert payload["prompt"] not in args
    assert "_pollard" not in args


def test_workspace_write_and_model_are_explicit(fake_cli: Any, payload: Any) -> None:
    setup, calls, _processes = fake_cli
    payload.update({"sandbox": "workspace-write", "model": "test-model"})
    setup(output_program(protocol(EVENTS)))(payload)
    args = calls[0][0]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[-3:] == ["--model", "test-model", "-"]
    assert not any("bypass" in arg for arg in args)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"prompt": ""}, ValueError),
        ({"prompt": "\ud800"}, UnicodeEncodeError),
        ({"prompt": "with\0nul"}, ValueError),
        ({"cwd": "relative"}, ValueError),
        ({"workspace_fingerprint": ""}, ValueError),
        ({"workspace_fingerprint": None}, ValueError),
        ({"sandbox": "danger-full-access"}, ValueError),
        ({"model": None}, ValueError),
        ({"max_tokens": 20}, ValueError),
        ({"config": {}}, ValueError),
    ],
)
def test_invalid_payload_never_dispatches(
    fake_cli: Any, payload: Any, change: dict[str, Any], error: type[Exception]
) -> None:
    setup, calls, _processes = fake_cli
    payload.update(change)
    with pytest.raises(error) as raised:
        setup(output_program(protocol(EVENTS)))(payload)
    assert not is_post_dispatch_outcome_unknown(raised.value)
    assert calls == []


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": 0}, {"timeout": float("inf")}, {"timeout": float("nan")},
        {"timeout": True}, {"max_output_bytes": 0}, {"max_output_bytes": True},
        {"max_output_bytes": 2.5}, {"env": {"KEY": None}}, {"env": {"BAD=KEY": "x"}},
    ],
)
def test_invalid_factory_options(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_codex_exec_fn(**options)


def test_missing_binary_is_actionable_and_not_dispatched(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    monkeypatch.setattr(codex, "_bundled_paths", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    fn = make_codex_exec_fn()
    with pytest.raises(FileNotFoundError, match=r"pollard\[codex\]") as raised:
        fn(payload)
    assert not is_post_dispatch_outcome_unknown(raised.value)


def test_spawn_failure_is_not_dispatched(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("sensitive platform detail")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(CodexExecError) as raised:
        make_codex_exec_fn(executable=sys.executable)(payload)
    assert raised.value.code == "spawn_failed"
    assert "sensitive" not in str(raised.value)
    assert not is_post_dispatch_outcome_unknown(raised.value)


@pytest.mark.parametrize(
    ("output", "status", "code"),
    [
        (protocol(EVENTS), 7, "exit_status"),
        (b"not json; sensitive detail\n", 0, "invalid_json"),
        (b'[{"type":"turn.completed"}]\n', 0, "invalid_event"),
        (b'{"type":"turn.completed","usage":NaN}\n', 0, "invalid_json"),
        (b"\xff\n", 0, "invalid_json"),
        (protocol(EVENTS[:-1]), 0, "incomplete"),
        (protocol(EVENTS)[:-4], 0, "invalid_json"),
        (protocol([*EVENTS, EVENTS[-1]]), 0, "invalid_event"),
        (protocol([{"type": "turn.failed", "error": {"message": "secret"}}]), 0, "turn.failed"),
        (protocol([{"type": "error", "message": "secret"}]), 0, "error"),
    ],
)
def test_protocol_failures_are_safe_post_dispatch_errors(
    fake_cli: Any, payload: Any, output: bytes, status: int, code: str
) -> None:
    setup, calls, processes = fake_cli
    fn = setup(output_program(output, stderr=b"sensitive secret stderr", status=status))
    with pytest.raises(CodexExecError) as raised:
        fn(payload)
    assert raised.value.code == code
    assert is_post_dispatch_outcome_unknown(raised.value)
    assert "secret" not in str(raised.value)
    assert "sensitive" not in str(raised.value)
    assert processes[0].poll() is not None
    assert len(calls) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_caps_terminate_a_running_process(fake_cli: Any, payload: Any, stream: str) -> None:
    setup, _calls, processes = fake_cli
    program = (
        "import sys,time\n"
        "sys.stdin.buffer.read()\n"
        f"sys.{stream}.buffer.write(b'x'*65536)\n"
        f"sys.{stream}.buffer.flush()\n"
        "time.sleep(20)\n"
    )
    fn = setup(program, max_output_bytes=1024, timeout=5)
    with pytest.raises(CodexExecError, match=stream) as raised:
        fn(payload)
    assert raised.value.code == "output_limit"
    assert is_post_dispatch_outcome_unknown(raised.value)
    assert processes[0].poll() is not None


def test_timeout_terminates_process_and_stalled_stdin(fake_cli: Any, payload: Any) -> None:
    setup, _calls, processes = fake_cli
    payload["prompt"] = "x" * (1024 * 1024)
    fn = setup("import time; time.sleep(20)", timeout=0.2)
    start = time.monotonic()
    with pytest.raises(CodexExecError) as raised:
        fn(payload)
    assert raised.value.code == "timeout"
    assert is_post_dispatch_outcome_unknown(raised.value)
    assert processes[0].poll() is not None
    assert time.monotonic() - start < 10


def test_chunked_utf8_and_missing_final_newline(fake_cli: Any, payload: Any) -> None:
    setup, _calls, _processes = fake_cli
    data = protocol(EVENTS).replace(b"\\u2713", "✓".encode()).rstrip(b"\n")
    program = (
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"for value in {data!r}:\n"
        " sys.stdout.buffer.write(bytes([value]));sys.stdout.buffer.flush()\n"
    )
    result = setup(program)(payload)
    assert result["text"] == "hello ✓"


@pytest.mark.parametrize(
    "usage",
    [None, {}, {"input_tokens": 5}, {**USAGE, "input_tokens": True},
     {**USAGE, "output_tokens": -1}, {**USAGE, "cached_input_tokens": "7"},
     {**USAGE, "reasoning_output_tokens": -2}],
)
def test_invalid_usage_is_never_fabricated(fake_cli: Any, payload: Any, usage: Any) -> None:
    setup, _calls, _processes = fake_cli
    events = [*EVENTS[:-1], {"type": "turn.completed", "usage": usage}]
    result = setup(output_program(protocol(events)))(payload)
    assert "usage" not in result
    if isinstance(usage, dict):
        assert result["provider_usage"] == usage


class FixedEstimator:
    def estimate_input_tokens(self, _payload: dict[str, Any]) -> int:
        return 10


def test_runtime_record_replay_and_budget_charges(fake_cli: Any, payload: Any) -> None:
    setup, calls, _processes = fake_cli
    store = MemoryStore()
    fn = setup(output_program(protocol(EVENTS)))
    node = Runtime(store, meters=[StepMeter(), TokenMeter()]).run(
        "codex-record", budget=Budget(tokens=100, steps=2)
    ).model_call(
        payload, fn=fn
    )
    assert node.meta["charges"] == {"steps": 1, "tokens": 15}
    replay = Runtime(store, mode="replay").run("codex-record").model_call(
        payload, fn=make_codex_exec_fn(executable="missing-no-runtime-for-replay")
    )
    assert replay.result == node.result
    assert len(calls) == 1


def test_runtime_missing_usage_settles_estimate(fake_cli: Any, payload: Any) -> None:
    setup, _calls, _processes = fake_cli
    events = [*EVENTS[:-1], {"type": "turn.completed"}]
    runtime = Runtime(
        MemoryStore(), meters=[StepMeter(), TokenMeter(FixedEstimator(), reserved_output_tokens=5)]
    )
    with pytest.warns(UserWarning, match="no compatible usage"):
        node = runtime.run("codex-missing-usage", budget=Budget(tokens=20)).model_call(
            payload, fn=setup(output_program(protocol(events)))
        )
    assert node.meta["charges"] == {"steps": 1, "tokens": 15}
    assert node.meta["accounting_fallbacks"]["tokens"]["source"] == "precheck_estimate"


def test_runtime_dispatch_failure_settles_estimate(fake_cli: Any, payload: Any) -> None:
    setup, _calls, _processes = fake_cli
    runtime = Runtime(
        MemoryStore(), meters=[StepMeter(), TokenMeter(FixedEstimator(), reserved_output_tokens=5)]
    )
    run = runtime.run("codex-failed", budget=Budget(tokens=20))
    with pytest.raises(CodexExecError):
        run.model_call(payload, fn=setup(output_program(b"", status=1)))
    assert run.cursor.meta["charges"] == {"steps": 1, "tokens": 15}
    assert run.cursor.payload["event"] == "call_outcome_unknown"


def test_resolution_prefers_bundle_and_prepends_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    binary = Path(sys.executable).resolve()
    helper = binary.parent / "codex-helpers"
    monkeypatch.setattr(codex, "_bundled_paths", lambda: (binary, helper))
    monkeypatch.setenv("PATH", "ambient-path")
    assert codex.codex_executable() == binary
    env = codex.codex_environment({"PATH": "overridden-path"}, executable=binary)
    assert env["PATH"] == str(helper) + os.pathsep + "overridden-path"


def test_resolution_falls_back_to_native_path(monkeypatch: pytest.MonkeyPatch) -> None:
    binary = Path(sys.executable).resolve()
    monkeypatch.setattr(codex, "_bundled_paths", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda _name: str(binary))
    assert codex.codex_executable() == binary


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch-wrapper behavior")
def test_windows_rejects_shell_wrappers(tmp_path: Path) -> None:
    script = tmp_path / "codex.cmd"
    script.write_text("@echo unsafe", encoding="utf-8")
    with pytest.raises(ValueError, match="batch wrappers"):
        codex.codex_executable(script)
