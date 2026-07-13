"""Shared local-model and artifact helpers for the EXP-006 case studies."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from pollard import SQLiteStore, seal, verify
from pollard.cli import main as cli_main


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class _ChatCompletions:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def create(self, **params: Any) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("local chat completion did not return an object")
        return value


class _Chat:
    def __init__(self, base_url: str) -> None:
        self.completions = _ChatCompletions(base_url)


class LocalOpenAICompatibleClient:
    """Minimal client shape consumed by Pollard's OpenAI chat adapter."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat = _Chat(self.base_url)

    def health(self) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("llama.cpp health response is not an object")
        return value


@contextmanager
def local_llama_server(
    server_binary: Path,
    model: Path,
    *,
    port: int,
) -> Iterator[LocalOpenAICompatibleClient]:
    """Start one pinned local llama.cpp server with prompt caching disabled."""

    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="pollard-exp-006-") as temporary:
        log_path = Path(temporary) / "llama-server.log"
        with log_path.open("w", encoding="utf-8") as log:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [
                    str(server_binary),
                    "-m",
                    str(model),
                    "-ngl",
                    "99",
                    "-c",
                    "8192",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--parallel",
                    "1",
                    "--no-cache-prompt",
                    "--cache-ram",
                    "0",
                    "--no-cache-idle-slots",
                    "--no-webui",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=os.environ.copy(),
            )
            try:
                client = LocalOpenAICompatibleClient(base_url)
                _wait_for_server(client, process, log_path)
                yield client
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


def finalize_case(
    db_path: Path,
    root_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Verify a case and emit its full seal and content-free CLI HTML tree."""

    seal_path = output_dir / "seal.json"
    html_path = output_dir / "tree.html"
    for path in (seal_path, html_path):
        path.unlink(missing_ok=True)
    captured = StringIO()
    with redirect_stdout(captured):
        if cli_main(["verify", str(db_path), "--json"]) != 0:
            raise RuntimeError("pollard verify failed")
        if cli_main(["seal", str(db_path), root_id, "--output", str(seal_path)]) != 0:
            raise RuntimeError("pollard seal failed")
        if cli_main(["show", str(db_path), root_id, "--html", str(html_path)]) != 0:
            raise RuntimeError("pollard HTML export failed")
    with SQLiteStore(db_path) as store:
        reports = [verify(store, node.id) for node in store.walk(root_id)]
        report = seal(store, root_id)
        node_count = len(list(store.walk(root_id)))
    if not all(item.ok for item in reports):
        raise RuntimeError("case-study store is not verify-clean")
    document = json.loads(seal_path.read_text(encoding="utf-8"))
    if document["digest"] != report.digest:
        raise RuntimeError("CLI and API seal digests differ")
    return {
        "root_id": root_id,
        "seal_digest": report.digest,
        "seal_entries": len(report.entries),
        "node_count": node_count,
        "db_sha256": sha256_file(db_path),
        "seal_sha256": sha256_file(seal_path),
        "html_sha256": sha256_file(html_path),
        "cli_output": captured.getvalue().splitlines(),
    }


def _wait_for_server(
    client: LocalOpenAICompatibleClient,
    process: subprocess.Popen[Any],
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"llama-server exited early:\n{log_path.read_text(encoding='utf-8')}"
            )
        try:
            if client.health().get("status") == "ok":
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"llama-server readiness timed out:\n{log_path.read_text(encoding='utf-8')}")
