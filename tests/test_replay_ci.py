import runpy
import socket
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def test_replay_ci_example_uses_recording_with_sockets_guarded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]:
        monkeypatch.delenv(name, raising=False)

    def blocked_socket(*_args: Any, **_kwargs: Any) -> socket.socket:
        raise AssertionError("network socket opened during replay")

    monkeypatch.setattr(socket, "socket", blocked_socket)

    module = runpy.run_path(str(ROOT / "examples" / "05_replay_ci.py"))
    result = module["run_replay"]()

    assert result["text"].startswith("mock response ")
    assert result["avoided"]["steps"] == 1.0
