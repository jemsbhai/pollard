from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pollard import Runtime, SQLiteSealSink, SQLiteStore, seal


def test_sqlite_seal_sink_appends_complete_ordered_records(tmp_path: Path) -> None:
    runtime = Runtime()
    with runtime.run("custody") as run:
        run.note({"status": "ready"})
    report = seal(runtime.store, run.root_id)
    sink = SQLiteSealSink(tmp_path / "external-custody.db")

    first = sink.publish(
        report,
        store_id="support-prod",
        signer_identity="release-operator",
        sealed_at="2026-07-20T00:00:00Z",
    )
    second = sink.publish(
        report,
        store_id="support-prod",
        signer_identity="release-operator",
        sealed_at="2026-07-20T00:01:00Z",
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.root_id == report.root_id
    assert first.algorithm == report.algorithm
    assert first.digest == report.digest
    assert sink.records() == [first, second]


@pytest.mark.parametrize(("store_id", "signer"), [("", "operator"), ("prod", "")])
def test_sqlite_seal_sink_requires_custody_identity(
    tmp_path: Path,
    store_id: str,
    signer: str,
) -> None:
    runtime = Runtime()
    run = runtime.run("custody-validation")
    report = seal(runtime.store, run.root_id)
    sink = SQLiteSealSink(tmp_path / "external-custody.db")
    with pytest.raises(ValueError):
        sink.publish(report, store_id=store_id, signer_identity=signer)


def test_sqlite_seal_sink_refuses_pollard_store_database(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    with SQLiteStore(path):
        pass
    with pytest.raises(ValueError, match="must not use a Pollard store"):
        SQLiteSealSink(path)


def test_sqlite_seal_sink_sequences_concurrent_publications(tmp_path: Path) -> None:
    runtime = Runtime()
    run = runtime.run("concurrent-custody")
    report = seal(runtime.store, run.root_id)
    path = tmp_path / "external-custody.db"

    def publish(index: int) -> int:
        sink = SQLiteSealSink(path)
        return sink.publish(
            report,
            store_id="support-prod",
            signer_identity=f"operator-{index}",
        ).sequence

    with ThreadPoolExecutor(max_workers=5) as executor:
        sequences = list(executor.map(publish, range(10)))
    assert sorted(sequences) == list(range(1, 11))
