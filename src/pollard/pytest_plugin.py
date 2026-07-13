"""Pytest integration for pollard recordings."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.fixtures import FixtureRequest

if TYPE_CHECKING:
    from .replay import ReplayMode
    from .runtime import Run


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("pollard")
    group.addoption(
        "--pollard-mode",
        action="store",
        choices=["record", "hybrid", "replay"],
        default=None,
        help="pollard recording mode: record, replay, or hybrid.",
    )
    parser.addini(
        "pollard_recordings_dir",
        "Directory for pollard SQLite recording files.",
        default="tests/pollard_recordings",
    )
    parser.addini(
        "pollard_mode",
        "Default pollard recording mode when --pollard-mode is not set.",
        default="",
    )


@pytest.fixture
def pollard_run(request: FixtureRequest) -> Iterator[Run]:
    from .runtime import Runtime
    from .stores import SQLiteStore

    mode = _mode_from_config(request.config)
    recordings_dir = _recordings_dir(request.config)
    recordings_dir.mkdir(parents=True, exist_ok=True)
    db_path = recordings_dir / f"{_module_stem(request)}.db"
    with SQLiteStore(db_path) as store, Runtime(store, mode=mode).run(
        request.node.nodeid
    ) as run:
        yield run


def _mode_from_config(config: Config) -> ReplayMode:
    from .replay import ReplayMode, normalize_mode

    option = config.getoption("pollard_mode")
    if isinstance(option, str) and option:
        return normalize_mode(option)
    ini_mode = str(config.getini("pollard_mode")).strip()
    if ini_mode:
        return normalize_mode(ini_mode)
    if os.environ.get("CI"):
        return ReplayMode.REPLAY
    return ReplayMode.HYBRID


def _recordings_dir(config: Config) -> Path:
    raw = Path(str(config.getini("pollard_recordings_dir")))
    if raw.is_absolute():
        return raw
    return Path(config.rootpath) / raw


def _module_stem(request: FixtureRequest) -> str:
    path = getattr(request.node, "path", None)
    if path is not None:
        return Path(path).stem
    return Path(str(request.node.fspath)).stem
