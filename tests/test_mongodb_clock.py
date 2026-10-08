from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pollard.errors import IntegrityError
from pollard.stores.mongodb import MongoStore, _MongoTransaction, _server_timestamp


@pytest.mark.parametrize(
    "server_time",
    [
        datetime(2026, 1, 1, 0, 0, 0, 125000),
        datetime(2026, 1, 1, 0, 0, 0, 125000, tzinfo=timezone.utc),
        datetime(2025, 12, 31, 17, 0, 0, 125000, tzinfo=timezone(timedelta(hours=-7))),
        datetime(2026, 1, 1, 5, 30, 0, 125000, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
@pytest.mark.parametrize("clock_path", ["coordinator", "aggregate", "hello"])
def test_server_clock_uses_bson_utc_for_every_decode_mode(
    server_time: datetime, clock_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = 1767225600.125
    coordinator = {
        "_id": "store",
        "store_id": "store",
        "revision": 1,
        "locked_at": server_time,
    }

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def with_transaction(
            self, callback: Callable[[Session], float], **_options: object
        ) -> float:
            return callback(self)

    if clock_path == "coordinator":
        store = object.__new__(MongoStore)
        store.store_id = "store"
        store._client = SimpleNamespace(start_session=Session)
        store._records = object()
        monkeypatch.setattr(
            store,
            "_pymongo",
            SimpleNamespace(ReturnDocument=SimpleNamespace(AFTER="after")),
            raising=False,
        )
        store._coordinators = SimpleNamespace(
            find_one=lambda *_args, **_kwargs: coordinator,
            find_one_and_update=lambda *_args, **_kwargs: coordinator,
        )
        monkeypatch.setattr(store, "_transaction_options", lambda: ("read", "write", "primary"))
        assert store._write(lambda tx: tx.now()) == expected
    else:
        calls: list[str] = []

        def aggregate(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            calls.append("aggregate")
            return [{"now": server_time}] if clock_path == "aggregate" else []

        def hello(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("hello")
            return {"localTime": server_time}

        records = SimpleNamespace(aggregate=aggregate, database=SimpleNamespace(command=hello))
        tx = _MongoTransaction(records, object(), "store", timestamp=None)
        assert tx.now() == expected
        assert tx.now() == expected
        assert calls == (["aggregate"] if clock_path == "aggregate" else ["aggregate", "hello"])


@pytest.mark.parametrize("timezone_name", ["host", "UTC0", "MST7MDT", "IST-5:30"])
def test_naive_server_clock_in_a_separate_process_timezone(timezone_name: str) -> None:
    if not hasattr(time, "tzset") and timezone_name not in {"UTC0", "host"}:
        pytest.skip("Windows uses its configured host timezone instead of POSIX TZ")
    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import json,time
from datetime import datetime,timezone
from pollard.stores.mongodb import _server_timestamp
if hasattr(time,'tzset'):time.tzset()
naive=datetime(2026,1,1)
expected=datetime(2026,1,1,tzinfo=timezone.utc).timestamp()
assert _server_timestamp(naive)==expected
print(json.dumps({'local_offset':naive.timestamp()-expected,'fixed':_server_timestamp(naive)}))
"""
    env = {**os.environ, "PYTHONPATH": str(source)}
    if timezone_name != "host":
        env["TZ"] = timezone_name
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(result.stdout)
    assert values["fixed"] == 1767225600.0
    if hasattr(time, "tzset") and timezone_name != "host":
        assert values["local_offset"] == {"UTC0": 0, "MST7MDT": 25200, "IST-5:30": -19800}[
            timezone_name
        ]


@pytest.mark.parametrize(
    "invalid", [None, 1767225600.0, "2026-01-01", SimpleNamespace(timestamp=lambda: 1.0)]
)
def test_invalid_server_clock_does_not_accept_timestamp_lookalikes(invalid: object) -> None:
    with pytest.raises(IntegrityError, match="current time"):
        _server_timestamp(invalid)
