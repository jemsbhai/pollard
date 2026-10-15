"""Bounded, synchronous ``codex exec`` calls for Pollard model steps.

Each call is one fresh, ephemeral Codex task. Pollard governs that outer step;
Codex's own sandbox, approvals, configuration and internal retries still apply.
Replay returns the recorded answer, without recreating filesystem side effects.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from ._common import int_field, post_dispatch_boundary, set_normalized_usage

_INSTALL_HELP = "Install pollard[codex] or supply a native Codex executable."
_PAYLOAD_FIELDS = {"prompt", "cwd", "workspace_fingerprint", "model", "sandbox", "_pollard"}
_USAGE_DETAILS = ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")


class CodexExecError(RuntimeError):
    """A safe diagnostic that never includes Codex's stderr or raw event data."""

    def __init__(self, message: str, *, code: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.returncode = returncode


def _bundled_paths() -> tuple[Path, Path | None] | None:
    try:
        module = importlib.import_module("codex_cli_bin")
    except ModuleNotFoundError as error:
        if error.name != "codex_cli_bin":
            raise
        return None
    return Path(module.bundled_codex_path()), module.bundled_path_dir()


def codex_executable(executable: str | Path | None = None) -> Path:
    """Resolve a native binary, preferring the optional bundled runtime.

Resolution is read-only and does not authenticate or start Codex. Windows batch
wrappers are rejected because Windows can execute them through a shell even
when ``shell=False``. The bundled runtime supplies a native executable.
"""

    candidate: str | Path | None = executable
    if candidate is None:
        bundled = _bundled_paths()
        candidate = bundled[0] if bundled is not None else shutil.which("codex")
    elif not isinstance(candidate, (str, Path)) or not str(candidate).strip():
        raise ValueError("executable must be a nonempty string or Path")
    elif not Path(candidate).is_file():
        candidate = shutil.which(str(candidate))
    if candidate is None:
        raise FileNotFoundError(f"Codex CLI was not found. {_INSTALL_HELP}")
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError(f"Codex executable is missing or not executable. {_INSTALL_HELP}")
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        raise ValueError(f"Windows batch wrappers are not supported. {_INSTALL_HELP}")
    return path


def codex_environment(
    env: Mapping[str, str] | None = None, *, executable: Path | None = None
) -> dict[str, str]:
    """Copy the environment, apply overrides, and add bundled helper binaries.

Authentication is inherited from the usual Codex configuration or supplied
environment. Environment values are never added to Pollard's result or errors.
"""

    result = dict(os.environ)
    if env is not None:
        for key, value in env.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise ValueError("env must map valid environment names to string values")
            # Windows environment names are case-insensitive, including PATH.
            if os.name == "nt":
                for existing in list(result):
                    if existing.upper() == key.upper():
                        del result[existing]
            result[key] = value
    if executable is not None:
        bundled = _bundled_paths()
        if bundled is not None and executable.resolve() == bundled[0].resolve() and bundled[1]:
            path_key = next((key for key in result if key.upper() == "PATH"), "PATH")
            result[path_key] = str(bundled[1]) + os.pathsep + result.get(path_key, "")
    return result


def make_codex_exec_fn(
    *,
    executable: str | Path | None = None,
    timeout: float = 300,
    max_output_bytes: int = 8_388_608,
    env: Mapping[str, str] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a Pollard callable for one non-interactive Codex task.

    Required payload fields are ``prompt``, absolute existing ``cwd``, and a
    caller-owned ``workspace_fingerprint`` covering repository state and other
    execution context. Optional fields are ``model`` and ``sandbox`` (default
    ``read-only``; also supports ``workspace-write``). ``_pollard`` is reserved
    metadata and is not passed to Codex. Other fields are rejected.

    ``timeout`` bounds the subprocess lifetime; ``max_output_bytes`` independently
    caps stdout and stderr, not generated tokens. No automatic adapter retries
    occur. Failure cleanup kills the subprocess and attempts to kill descendants
    (process groups on POSIX; taskkill on Windows). Detached descendants may
    escape that best-effort cleanup. Raw stderr, reasoning and tool events are
    discarded. Usage counters are normalized only when valid; reported cache
    and reasoning details remain in ``provider_usage`` without double counting.

    Resolution happens inside the callable so recorded replay needs no CLI.
    """

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise ValueError("max_output_bytes must be a positive integer")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    overrides = dict(env) if env is not None else None
    codex_environment(overrides)  # Validate without resolving the optional runtime.

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        prompt, cwd, sandbox, model = _request(payload)
        prompt_bytes = prompt.encode("utf-8")
        binary = codex_executable(executable)
        process_env = codex_environment(overrides, executable=binary)
        args = [
            str(binary), "--no-daemon", "exec", "--json", "--ephemeral",
            "--sandbox", sandbox, "-C", cwd,
        ]
        if model is not None:
            args.extend(["--model", model])
        args.append("-")
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=process_env,
                shell=False,
                bufsize=0,
                start_new_session=os.name != "nt",
                creationflags=creation_flags,
            )
        except OSError:
            raise CodexExecError(
                f"Unable to start Codex CLI. {_INSTALL_HELP}", code="spawn_failed"
            ) from None
        with post_dispatch_boundary():
            try:
                return _collect(process, prompt_bytes, timeout, max_output_bytes)
            finally:
                if process.poll() is None:
                    _terminate(process)

    return call


def _request(payload: dict[str, Any]) -> tuple[str, str, str, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("Codex payload must be a dictionary")
    if payload.keys() - _PAYLOAD_FIELDS:
        raise ValueError("Unsupported Codex payload field; see make_codex_exec_fn")
    for name in ("prompt", "cwd", "workspace_fingerprint"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"{name} must be a nonempty string without NUL characters")
    cwd = Path(payload["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("cwd must be an absolute path to an existing directory")
    sandbox = payload.get("sandbox", "read-only")
    if sandbox not in ("read-only", "workspace-write"):
        raise ValueError("sandbox must be read-only or workspace-write")
    model = payload.get("model")
    if "model" in payload and (
        not isinstance(model, str) or not model.strip() or "\0" in model
    ):
        raise ValueError("model must be a nonempty string without NUL characters")
    return payload["prompt"], str(cwd), sandbox, model


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Best-effort tree cleanup; bounded waits and diagnostics never mask errors."""

    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(OSError):
        process.kill()
    with suppress(OSError, subprocess.SubprocessError):
        process.wait(timeout=5)


def _collect(
    process: subprocess.Popen[bytes], prompt: bytes, timeout: float, limit: int
) -> dict[str, Any]:
    messages: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def send(kind: str, data: bytes | None) -> None:
        while not stopped.is_set():
            try:
                messages.put((kind, data), timeout=0.05)
                return
            except queue.Full:
                pass

    def read(kind: str, pipe: BinaryIO) -> None:
        try:
            with pipe:
                while not stopped.is_set():
                    chunk = pipe.read(8192)
                    if not chunk:
                        break
                    send(kind, chunk)
        except OSError:
            send("io_error", None)
        finally:
            send(kind, None)

    def write(pipe: BinaryIO) -> None:
        try:
            with pipe:
                remaining = memoryview(prompt)
                while remaining and not stopped.is_set():
                    written = pipe.write(remaining[:8192])
                    if not written:
                        raise OSError("closed input")
                    remaining = remaining[written:]
        except BrokenPipeError:
            pass  # Exit status and terminal events decide whether this was successful.
        except OSError:
            send("io_error", None)

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    workers = [
        threading.Thread(target=read, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read, args=("stderr", process.stderr), daemon=True),
        threading.Thread(target=write, args=(process.stdin,), daemon=True),
    ]
    deadline = time.monotonic() + timeout
    parser = _EventParser()
    sizes = {"stdout": 0, "stderr": 0}
    closed: set[str] = set()
    succeeded = False
    try:
        for worker in workers:
            worker.start()
        while len(closed) < 2 or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexExecError("Codex CLI exceeded its timeout", code="timeout")
            try:
                kind, chunk = messages.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if kind == "io_error":
                raise CodexExecError("Codex CLI pipe communication failed", code="io_error")
            if chunk is None:
                closed.add(kind)
                continue
            sizes[kind] += len(chunk)
            if sizes[kind] > limit:
                raise CodexExecError(
                    f"Codex CLI {kind} exceeded max_output_bytes", code="output_limit"
                )
            if kind == "stdout":
                parser.feed(chunk)
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if returncode:
            raise CodexExecError(
                f"Codex CLI exited with status {returncode}; check Codex authentication and setup",
                code="exit_status",
                returncode=returncode,
            )
        result = parser.finish()
        succeeded = True
        return result
    finally:
        stopped.set()
        if not succeeded:
            _terminate(process)
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=0.2)


class _EventParser:
    def __init__(self) -> None:
        self.pending = bytearray()
        self.thread_id: str | None = None
        self.text: str | None = None
        self.completed = False
        self.usage: Any = None

    def feed(self, chunk: bytes) -> None:
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending = rest
            if line.strip():
                self._event(line)

    def _event(self, line: bytes | bytearray) -> None:
        try:
            event = json.loads(line, parse_constant=_invalid_json_constant)
        except (ValueError, UnicodeError):
            raise CodexExecError("Codex CLI emitted invalid JSONL", code="invalid_json") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise CodexExecError("Codex CLI emitted an invalid event", code="invalid_event")
        kind = event["type"]
        if kind in {"error", "turn.failed"}:
            raise CodexExecError(f"Codex CLI reported {kind}", code=kind)
        if self.completed:
            raise CodexExecError(
                "Codex CLI emitted events after turn.completed", code="invalid_event"
            )
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if self.thread_id is not None or not isinstance(thread_id, str) or not thread_id:
                raise CodexExecError("Codex CLI emitted an invalid thread ID", code="invalid_event")
            self.thread_id = thread_id
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise CodexExecError("Codex CLI emitted an invalid item", code="invalid_event")
            if item.get("type") == "agent_message":
                if not isinstance(item.get("text"), str):
                    raise CodexExecError(
                        "Codex CLI emitted invalid answer text", code="invalid_event"
                    )
                self.text = item["text"]
        elif kind == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")

    def finish(self) -> dict[str, Any]:
        if self.pending.strip():
            # A complete final JSON value is valid even without a trailing LF.
            self._event(self.pending)
        if not self.completed or self.thread_id is None or self.text is None:
            raise CodexExecError("Codex CLI ended without a complete answer", code="incomplete")
        result: dict[str, Any] = {
            "text": self.text,
            "thread_id": self.thread_id,
            "usage": self.usage,
        }
        set_normalized_usage(
            result,
            {
                "input_tokens": int_field(self.usage, "input_tokens"),
                "output_tokens": int_field(self.usage, "output_tokens"),
            },
            required_fields=(("input_tokens",), ("output_tokens",)),
            optional_fields=_USAGE_DETAILS,
        )
        return result


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")
