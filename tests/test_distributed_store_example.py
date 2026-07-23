from __future__ import annotations

import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pollard import BudgetExceeded

ROOT = Path(__file__).resolve().parents[1]


class _Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.result = object()

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.result


def _example() -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "examples" / "09_distributed_stores.py"))


@pytest.mark.parametrize(
    ("backend", "class_name", "environment", "expected_args", "expected_kwargs"),
    [
        (
            "postgresql",
            "PostgresStore",
            {"POLLARD_PG_DSN": "postgresql://example"},
            ("postgresql://example",),
            {"store_id": "team"},
        ),
        (
            "redis",
            "RedisStore",
            {
                "POLLARD_REDIS_URL": "rediss://example",
                "POLLARD_REDIS_PREFIX": "ledger",
            },
            ("rediss://example",),
            {"store_id": "team", "prefix": "ledger"},
        ),
        (
            "mongodb",
            "MongoStore",
            {
                "POLLARD_MONGODB_URI": "mongodb://example",
                "POLLARD_MONGODB_DATABASE": "ledger",
            },
            ("mongodb://example",),
            {"database": "ledger", "store_id": "team"},
        ),
        (
            "neo4j",
            "Neo4jStore",
            {
                "POLLARD_NEO4J_URI": "neo4j+s://example",
                "POLLARD_NEO4J_USER": "pollard_app",
                "POLLARD_NEO4J_PASSWORD": "test-password",
                "POLLARD_NEO4J_DATABASE": "ledger",
            },
            ("neo4j+s://example", ("pollard_app", "test-password")),
            {"database": "ledger", "store_id": "team"},
        ),
        (
            "kafka",
            "KafkaStore",
            {
                "POLLARD_KAFKA_BOOTSTRAP": "broker.example:9092",
                "POLLARD_KAFKA_TOPIC": "pollard-team",
            },
            ({"bootstrap.servers": "broker.example:9092"},),
            {"topic": "pollard-team", "store_id": "team"},
        ),
    ],
)
def test_distributed_store_example_maps_environment_to_constructor(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    class_name: str,
    environment: dict[str, str],
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
) -> None:
    module = _example()
    factory = _Factory()
    open_store = module["_open_store"]
    open_store.__globals__[class_name] = factory
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert open_store(backend, "team") is factory.result
    assert factory.calls == [(expected_args, expected_kwargs)]


def test_distributed_store_example_refuses_missing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _example()
    monkeypatch.delenv("POLLARD_KAFKA_BOOTSTRAP", raising=False)
    open_store: Callable[[str, str], object] = module["_open_store"]

    with pytest.raises(RuntimeError, match="POLLARD_KAFKA_BOOTSTRAP"):
        open_store("kafka", "team")


def test_distributed_store_example_refuses_unknown_backend() -> None:
    module = _example()
    open_store: Callable[[str, str], object] = module["_open_store"]

    with pytest.raises(ValueError, match="unsupported backend"):
        open_store("unknown", "team")


def test_distributed_store_example_explains_persistent_budget_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _example()

    class Store:
        def __enter__(self) -> Store:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Run:
        root_id = "root"

        def __enter__(self) -> Run:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def model_call(self, *_args: object, **_kwargs: object) -> None:
            raise BudgetExceeded("budget exceeded for steps", "refusal")

    class FakeRuntime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self, *_args: object, **_kwargs: object) -> Run:
            return Run()

    main = module["main"]
    main.__globals__["_open_store"] = lambda *_args: Store()
    main.__globals__["Runtime"] = FakeRuntime
    monkeypatch.setattr(
        sys,
        "argv",
        ["09_distributed_stores.py", "--backend", "redis"],
    )

    with pytest.raises(SystemExit, match="2"):
        main()
    stderr = capsys.readouterr().err
    assert "already spent its one-step budget" in stderr
    assert "--run-label" in stderr
