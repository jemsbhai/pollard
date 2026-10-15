import io
import json
from pathlib import Path
from typing import Any

import pytest

from pollard import codex_cli


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--prompt",
        "Review the fixture",
        "--cwd",
        str(tmp_path),
        "--workspace-fingerprint",
        "fixture-config-v1",
        "--run-label",
        "codex-test",
        "--store",
        str(tmp_path / "recording.db"),
        *extra,
    ]


def test_cli_records_and_replays_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, Any]] = []

    def execute(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {
            "text": "reviewed",
            "thread_id": "fixture",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 8,
            },
        }

    monkeypatch.setattr(codex_cli, "make_codex_exec_fn", lambda **_: execute)
    assert codex_cli.main(_args(tmp_path)) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["result"]["text"] == "reviewed"
    assert record["report"]["spent"]["tokens"] == 20
    assert calls[0]["sandbox"] == "read-only"

    def no_dispatch(_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("replay or duplicate reached the executable")

    monkeypatch.setattr(codex_cli, "make_codex_exec_fn", lambda **_: no_dispatch)
    assert codex_cli.main(_args(tmp_path, "--mode", "replay")) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["node_id"] == record["node_id"]
    assert replay["result"] == record["result"]
    assert replay["report"]["avoided"]["tokens"] == 20
    assert codex_cli.main(_args(tmp_path)) == 2
    assert "DuplicateRecording" in capsys.readouterr().err
    assert (
        codex_cli.main(_args(tmp_path, "--workspace-fingerprint", "changed", "--mode", "replay"))
        == 2
    )
    assert "MissingRecording" in capsys.readouterr().err


def test_cli_zero_steps_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_dispatch(_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("budget refusal reached the executable")

    monkeypatch.setattr(codex_cli, "make_codex_exec_fn", lambda **_: no_dispatch)
    assert codex_cli.main(_args(tmp_path, "--steps", "0")) == 2
    assert "BudgetExceeded" in capsys.readouterr().err


def test_cli_utf8_stdin_and_unicode_output_on_legacy_windows_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt = "Review caf\u00e9 \u65e5\u672c\u8a9e"
    answer = "\u65e5\u672c\u8a9e \u2705"
    input_stream = io.TextIOWrapper(io.BytesIO(prompt.encode("utf-8")), encoding="cp1252")
    output_bytes = io.BytesIO()
    output_stream = io.TextIOWrapper(output_bytes, encoding="cp1252", write_through=True)
    monkeypatch.setattr(codex_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(codex_cli.sys, "stdout", output_stream)

    def execute(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["prompt"] == prompt
        return {"text": answer, "usage": {"input_tokens": 2, "output_tokens": 3}}

    monkeypatch.setattr(codex_cli, "make_codex_exec_fn", lambda **_: execute)
    argv = _args(tmp_path)
    del argv[1:3]
    assert codex_cli.main(argv) == 0
    assert json.loads(output_bytes.getvalue())["result"]["text"] == answer


@pytest.mark.parametrize(
    "command,tail",
    [
        ("login", ["login"]),
        ("status", ["login", "status"]),
        ("version", ["--version"]),
    ],
)
def test_cli_auth_and_version_launch_selected_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    tail: list[str],
) -> None:
    binary = tmp_path / "codex"
    calls = []
    monkeypatch.setattr(codex_cli, "codex_executable", lambda _: binary)
    monkeypatch.setattr(codex_cli, "codex_environment", lambda **_: {"PATH": "fixture"})

    def launch(argv: list[str], **options: Any) -> int:
        calls.append((argv, options))
        return 7

    monkeypatch.setattr(codex_cli.subprocess, "call", launch)
    assert codex_cli.main([command]) == 7
    assert calls == [([str(binary), *tail], {"env": {"PATH": "fixture"}})]


@pytest.mark.parametrize(
    "extra", [["--steps", "-1"], ["--tokens", "-1"], ["--attempt", "-1"], ["--prompt", " "]]
)
def test_cli_invalid_arguments_fail_before_recording(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        codex_cli.main(_args(tmp_path, *extra))
    assert error.value.code == 2
    assert not (tmp_path / "recording.db").exists()


def test_bundled_codex_version_is_available_without_auth() -> None:
    pytest.importorskip("codex_cli_bin")
    import subprocess

    from pollard.adapters.codex import codex_environment, codex_executable

    binary = codex_executable()
    result = subprocess.run(
        [str(binary), "--version"],
        env=codex_environment(executable=binary),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "0.162.1" in result.stdout
