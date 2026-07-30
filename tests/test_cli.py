import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest

import pollard.cli as cli_module
from pollard import (
    MemoryStore,
    MongoStore,
    Neo4jStore,
    PostgresStore,
    RedisStore,
    Runtime,
    SQLiteStore,
    redact,
)
from pollard.cli import main, render_html
from pollard.tree import Node, NodeKind


def _recording(path: Path) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "private prompt"}],
    }
    with SQLiteStore(path) as store:
        run = Runtime(store).run("cli-test")
        run.model_call(
            payload,
            fn=lambda _payload: {
                "text": "private result",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
        return run.root_id, payload


def test_show_is_ascii_and_content_free_by_default(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "run.db"
    root_id, _payload = _recording(db)

    assert main(["show", str(db), root_id]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    output.encode("ascii")
    assert "model_call" in output
    assert "private prompt" not in output
    assert "private result" not in output

    assert main(["show", str(db), root_id, "--payloads"]) == 0
    private_output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "private prompt" in private_output
    assert "private result" in private_output

    assert main(["show", str(db), root_id, "--unicode"]) == 0
    unicode_output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "└─" in unicode_output


def test_runs_json_works_directly_and_in_a_subprocess(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "run.db"
    root_id, _payload = _recording(db)

    assert main(["runs", str(db), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert document["runs"][0]["root_id"] == root_id

    completed = subprocess.run(
        [sys.executable, "-m", "pollard.cli", "runs", str(db), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout)["runs"][0]["label"] == "cli-test"


def test_cli_machine_outputs_html_and_error_paths(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "run.db"
    root_id, _payload = _recording(db)

    assert main(["show", str(db), root_id, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert "payload" not in shown["nodes"][1]

    html_path = tmp_path / "run.html"
    assert main(["show", str(db), root_id, "--html", str(html_path), "--json"]) == 0
    html_outcome = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert html_outcome["bytes"] == len(html_path.read_bytes())
    assert "private prompt" not in html_path.read_text(encoding="utf-8")

    private_html = tmp_path / "private.html"
    assert (
        main(
            [
                "show",
                str(db),
                root_id,
                "--html",
                str(private_html),
                "--payloads",
            ]
        )
        == 0
    )
    capsys.readouterr()  # type: ignore[attr-defined]
    assert "private prompt" in private_html.read_text(encoding="utf-8")

    assert main(["runs", str(tmp_path / "missing.db")]) == 2
    assert "missing.db" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_human_readable_report_runs_verify_and_seal(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "run.db"
    root_id, _payload = _recording(db)

    assert main(["report", str(db), root_id]) == 0
    assert "spent:" in capsys.readouterr().out  # type: ignore[attr-defined]
    assert main(["verify", str(db)]) == 0
    assert "OK:" in capsys.readouterr().out  # type: ignore[attr-defined]
    assert main(["seal", str(db), root_id]) == 0
    assert len(capsys.readouterr().out.strip()) == 64  # type: ignore[attr-defined]
    assert main(["runs", str(db)]) == 0
    assert "cli-test" in capsys.readouterr().out  # type: ignore[attr-defined]

    empty = tmp_path / "empty.db"
    with SQLiteStore(empty):
        pass
    assert main(["runs", str(empty)]) == 0
    assert capsys.readouterr().out.strip() == "no runs"  # type: ignore[attr-defined]


def test_report_includes_persisted_replay_avoidance(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "run.db"
    root_id, payload = _recording(db)
    with SQLiteStore(db) as store:
        replay = Runtime(store, mode="hybrid").run("cli-test")
        replay.model_call(payload, fn=lambda _payload: {"text": "not called"})

    assert main(["report", str(db), root_id, "--json"]) == 0
    document = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert document["spent"]["tokens"] == 3.0
    assert document["avoided"]["steps"] == 1
    assert document["avoided"]["tokens"] == 3


def test_verify_exit_code_and_seal_output(tmp_path: Path, capsys: object) -> None:
    db = tmp_path / "run.db"
    root_id, _payload = _recording(db)

    assert main(["verify", str(db), root_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True  # type: ignore[attr-defined]

    report_path = tmp_path / "seal.json"
    assert main(["seal", str(db), root_id, "--output", str(report_path), "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert outcome["output"] == str(report_path)
    assert json.loads(report_path.read_text(encoding="utf-8"))["digest"] == outcome["digest"]

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE nodes SET result_digest = ? WHERE kind = ?", ("0" * 64, "model_call"))
    assert main(["verify", str(db), root_id, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False  # type: ignore[attr-defined]


def test_html_export_matches_golden_and_omits_content() -> None:
    store, root = _golden_tree()
    rendered = render_html(store, root.id)
    golden = Path(__file__).with_name("golden").joinpath("cli_tree.html")
    assert rendered == golden.read_text(encoding="utf-8")
    assert "private prompt" not in rendered
    assert "private result" not in rendered
    assert "<script" not in rendered


def test_html_export_of_one_thousand_nodes_has_a_size_guard() -> None:
    store = MemoryStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "large"})
    store.put(root)
    for index in range(1_000):
        store.put(
            Node.make(
                kind=NodeKind.NOTE,
                parent=root.id,
                payload={"label": f"node-{index}"},
            )
        )
    rendered = render_html(store, root.id)
    assert len(rendered.encode("utf-8")) < 1_000_000


def test_redaction_markers_and_governance_commands(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "governance.db"
    with SQLiteStore(db) as store:
        run = Runtime(store).run("governance-cli")
        run.note({"token": redact("never-store-this", hint="api token")})
        run.note({"label": "discard"})
        run.prune()
        root_id = run.root_id

    assert main(["show", str(db), root_id, "--payloads"]) == 0
    shown = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "[REDACTED]" in shown
    assert "never-store-this" not in shown

    assert main(["show", str(db), root_id, "--json", "--payloads"]) == 0
    document = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert document["nodes"][1]["redacted"] is True

    html = tmp_path / "redacted.html"
    assert main(["show", str(db), root_id, "--html", str(html), "--payloads"]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]
    assert 'class="redacted"' in html.read_text(encoding="utf-8")

    exported = tmp_path / "subtree.json"
    assert main(["export", str(db), root_id, str(exported), "--json"]) == 0
    export_result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert export_result["nodes"] == 3

    imported_db = tmp_path / "imported.db"
    assert main(["import", str(exported), str(imported_db), "--json"]) == 0
    import_result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert import_result["imported"] == 3
    assert main(["verify", str(imported_db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True  # type: ignore[attr-defined]

    assert main(["gc", str(db), "drop-pruned", "--json"]) == 0
    gc_result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert gc_result["removed_nodes"] == 1
    assert main(["gc", str(db), "compact", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "compact"  # type: ignore[attr-defined]


def test_cli_import_reports_tampered_manifest_without_writing(
    tmp_path: Path,
    capsys: object,
) -> None:
    db = tmp_path / "source.db"
    root_id, _payload = _recording(db)
    exported = tmp_path / "tampered.json"
    assert main(["export", str(db), root_id, str(exported)]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]
    manifest = json.loads(exported.read_text(encoding="utf-8"))
    manifest["seal"]["digest"] = "0" * 64
    exported.write_text(json.dumps(manifest), encoding="utf-8")

    target = tmp_path / "target.db"
    assert main(["import", str(exported), str(target)]) == 2
    assert "seal" in capsys.readouterr().err  # type: ignore[attr-defined]
    with SQLiteStore(target) as store:
        assert store.roots() == []


@pytest.mark.parametrize("command", ["import", "gc"])
@pytest.mark.parametrize(
    "remote_spec",
    [
        "redis-env:UNSET_REDIS_URL#team",
        "redis://private-user:private-password@redis.example/0#team",
        "pg-env:UNSET_POSTGRES_DSN#team",
        "postgresql://private-user:private-password@database.example/db#team",
        "mongo-env:UNSET_MONGODB_URI#team",
        "mongodb://private-user:private-password@mongo.example/db",
        "mongodb+srv://private-user:private-password@mongo.example/db",
        (
            "neo4j-env:UNSET_NEO4J_URI"
            "?user-env=UNSET_NEO4J_USER"
            "&password-env=UNSET_NEO4J_PASSWORD#team"
        ),
        "neo4j://private-user:private-password@neo4j.example",
    ],
)
def test_cli_sqlite_only_commands_reject_remote_targets_without_touching_files(
    command: str,
    remote_spec: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = [command, remote_spec, "--json"]
    if command == "import":
        source = tmp_path / "source.db"
        root_id, _payload = _recording(source)
        manifest = tmp_path / "subtree.json"
        assert main(["export", str(source), root_id, str(manifest), "--json"]) == 0
        capsys.readouterr()
        arguments = ["import", str(manifest), remote_spec, "--json"]

    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert main(arguments) == 2
    captured = capsys.readouterr()
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert captured.out == ""
    assert "accepts only a SQLite path" in captured.err
    assert after == before
    for secret in ("private-user", "private-password"):
        assert secret not in captured.err


def test_cli_runs_accepts_multiple_stores_and_merge_unions_them(
    tmp_path: Path,
    capsys: object,
) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    destination = tmp_path / "destination.db"
    first_root, _payload = _recording(first)
    with SQLiteStore(second) as store:
        second_run = Runtime(store).run("second-run")
        second_run.note({"label": "from-second"})

    assert main(["runs", str(first), str(second), "--json"]) == 0
    runs = json.loads(capsys.readouterr().out)["runs"]  # type: ignore[attr-defined]
    assert {run["label"] for run in runs} == {"cli-test", "second-run"}
    assert {run["store"] for run in runs} == {str(first), str(second)}

    assert (
        main(
            [
                "merge",
                str(destination),
                str(first),
                str(second),
                "--json",
            ]
        )
        == 0
    )
    merged = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert merged["copied"] == 4
    with SQLiteStore(destination) as store:
        assert store.exists(first_root)
        assert {store.get(root_id).payload["run"] for root_id in store.roots()} == {
            "cli-test",
            "second-run",
        }


def test_cli_merge_spools_multiple_large_sources_to_private_disk_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sources: list[Path] = []
    for source_index in range(3):
        path = tmp_path / f"large-source-{source_index}.db"
        sources.append(path)
        with SQLiteStore(path) as store:
            run = Runtime(store).run(f"large-{source_index}")
            for node_index in range(32):
                run.note(
                    {
                        "index": node_index,
                        "body": f"{source_index}:{node_index}:" + "x" * 8_192,
                    }
                )

    sentinel = tmp_path / "pollard-merge-sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    directories: list[Path] = []
    prepared: list[tuple[Path, int, set[str]]] = []
    real_prepare = cli_module._prepare_merge_source
    real_cleanup = cli_module._cleanup_merge_spool_directory

    def new_directory() -> Path:
        directory = tmp_path / f"private-spool-{len(directories)}"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    def track_prepare(spec: str, path: Path) -> object:
        spool = real_prepare(spec, path)
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        prepared.append((path, path.stat().st_size, set(vars(spool))))
        return spool

    def rename_then_cleanup(directory: Path) -> None:
        renamed = directory.with_name(f"{directory.name}-renamed")
        directory.rename(renamed)
        real_cleanup(renamed)

    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    monkeypatch.setattr(cli_module, "_prepare_merge_source", track_prepare)
    monkeypatch.setattr(
        cli_module,
        "_cleanup_merge_spool_directory",
        rename_then_cleanup,
    )
    destination = tmp_path / "large-destination.db"

    assert main(
        ["merge", str(destination), *(str(path) for path in sources), "--json"]
    ) == 0
    document = json.loads(capsys.readouterr().out)

    assert document["copied"] == 99
    assert len(prepared) == 3
    assert len({path for path, _size, _fields in prepared}) == 3
    assert all(size > 200_000 for _path, size, _fields in prepared)
    assert all(
        fields == {"path", "count", "digest"}
        for _path, _size, fields in prepared
    )
    assert all(not directory.exists() for directory in directories)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_cli_merge_finishes_every_source_before_destination_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    first = MemoryStore()
    first_root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "first"},
    )
    first.put(first_root)
    second_root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "second"},
    )

    class FailingSource:
        def roots(self) -> list[str]:
            events.append("second-roots")
            return [second_root.id]

        def walk(self, _root_id: str) -> Iterator[Node]:
            events.append("second-walk")
            yield second_root
            raise OSError("injected traversal failure")

    @contextmanager
    def fake_open_store(
        spec: str,
        *,
        create: bool,
        initialize_if_missing: bool = False,
    ) -> Iterator[object]:
        del initialize_if_missing
        events.append(f"open:{spec}:{create}")
        if spec == "first-source":
            try:
                yield first
            finally:
                events.append("close:first-source")
            return
        if spec == "second-source":
            try:
                yield FailingSource()
            finally:
                events.append("close:second-source")
            return
        raise AssertionError("destination was accessed before source preflight")

    directories: list[Path] = []

    def new_directory() -> Path:
        directory = tmp_path / "failure-spool"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    monkeypatch.setattr(cli_module, "_open_store", fake_open_store)
    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)

    assert (
        main(
            [
                "merge",
                "redis-env:DESTINATION_SECRET?prefix=pollard#team",
                "first-source",
                "second-source",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "could not prepare merge source second-source" in captured.err
    assert "DESTINATION_SECRET" not in captured.err
    assert events == [
        "open:first-source:False",
        "close:first-source",
        "open:second-source:False",
        "second-roots",
        "second-walk",
        "close:second-source",
    ]
    assert all(not directory.exists() for directory in directories)


def test_cli_merge_refuses_corrupt_finalized_spool_before_destination_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "corrupt-source.db"
    _recording(source)
    real_prepare = cli_module._prepare_merge_source
    real_open_store = cli_module._open_store
    real_cleanup = cli_module._cleanup_merge_spool_directory
    destination_accessed = False
    directories: list[Path] = []
    cleanup_calls: list[Path] = []

    def corrupt_prepare(spec: str, path: Path) -> object:
        spool = real_prepare(spec, path)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE metadata SET value = 'writing' WHERE key = 'state'"
            )
            connection.commit()
        finally:
            connection.close()
        return spool

    @contextmanager
    def guarded_open_store(
        spec: str,
        *,
        create: bool,
        initialize_if_missing: bool = False,
    ) -> Iterator[object]:
        nonlocal destination_accessed
        if create:
            destination_accessed = True
            raise AssertionError("destination was accessed for a corrupt spool")
        with real_open_store(
            spec,
            create=create,
            initialize_if_missing=initialize_if_missing,
        ) as store:
            yield store

    def new_directory() -> Path:
        directory = tmp_path / "corrupt-spool-directory"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    def track_cleanup(directory: Path) -> None:
        cleanup_calls.append(directory)
        real_cleanup(directory)

    monkeypatch.setattr(cli_module, "_prepare_merge_source", corrupt_prepare)
    monkeypatch.setattr(cli_module, "_open_store", guarded_open_store)
    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    monkeypatch.setattr(cli_module, "_cleanup_merge_spool_directory", track_cleanup)

    assert main(
        [
            "merge",
            str(tmp_path / "untouched.db"),
            str(source),
            "--json",
        ]
    ) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "could not finalize merge source" in captured.err
    assert "not finalized" in captured.err
    assert destination_accessed is False
    assert cleanup_calls == directories
    assert all(not directory.exists() for directory in directories)


def test_cli_merge_cleanup_failure_is_credential_safe_and_fails_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "cleanup-source.db"
    destination = tmp_path / "cleanup-destination.db"
    _recording(source)
    directories: list[Path] = []

    def new_directory() -> Path:
        directory = tmp_path / "cleanup-spool"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    def fail_cleanup(_directory: Path) -> None:
        raise OSError("raw private cleanup path")

    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    monkeypatch.setattr(cli_module, "_cleanup_merge_spool_directory", fail_cleanup)
    try:
        assert main(["merge", str(destination), str(source), "--json"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            "pollard: could not remove private merge preparation data\n"
        )
        with SQLiteStore(destination, read_only=True) as store:
            assert len(store.roots()) == 1
    finally:
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)


def test_cli_merge_preserves_primary_attribution_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "combined-failure-spool"

    def new_directory() -> Path:
        directory.mkdir(mode=0o700)
        return directory

    def fail_prepare(_spec: str, _path: Path) -> object:
        raise OSError("could not prepare merge source safe-source")

    def fail_cleanup(_directory: Path) -> None:
        raise OSError("raw cleanup details")

    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    monkeypatch.setattr(cli_module, "_prepare_merge_source", fail_prepare)
    monkeypatch.setattr(cli_module, "_cleanup_merge_spool_directory", fail_cleanup)
    try:
        assert main(
            [
                "merge",
                str(tmp_path / "unused-destination.db"),
                "safe-source",
                "--json",
            ]
        ) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "could not prepare merge source safe-source" in captured.err
        assert "private merge preparation cleanup also failed" in captured.err
        assert "raw cleanup details" not in captured.err
    finally:
        if directory.exists():
            shutil.rmtree(directory)


@pytest.mark.parametrize("failure_stage", ["spool-write", "destination-constructor"])
def test_cli_merge_cleans_private_spools_on_preparation_and_destination_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
) -> None:
    source = tmp_path / f"{failure_stage}-source.db"
    _recording(source)
    destination = tmp_path / f"{failure_stage}-destination.db"
    directories: list[Path] = []
    destination_accessed = False

    def new_directory() -> Path:
        directory = tmp_path / f"{failure_stage}-spool"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    if failure_stage == "spool-write":

        def fail_prepare(_spec: str, _path: Path) -> object:
            raise OSError("injected disk-full failure")

        monkeypatch.setattr(cli_module, "_prepare_merge_source", fail_prepare)
    else:
        real_open_store = cli_module._open_store

        @contextmanager
        def fail_destination(
            spec: str,
            *,
            create: bool,
            initialize_if_missing: bool = False,
        ) -> Iterator[object]:
            nonlocal destination_accessed
            if create:
                destination_accessed = True
                raise OSError("could not access destination store")
            with real_open_store(
                spec,
                create=create,
                initialize_if_missing=initialize_if_missing,
            ) as store:
                yield store

        monkeypatch.setattr(cli_module, "_open_store", fail_destination)

    assert main(["merge", str(destination), str(source), "--json"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "injected disk-full failure" in captured.err or (
        "could not access destination store" in captured.err
    )
    assert destination_accessed is (failure_stage == "destination-constructor")
    assert all(not directory.exists() for directory in directories)
    assert not destination.exists()


def test_cli_merge_partial_destination_failure_converges_on_exact_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "partial-source.db"
    root_id, _payload = _recording(source)
    backing = MemoryStore()
    invocation = 0
    directories: list[Path] = []

    class PartialDestination:
        def exists(self, node_id: str) -> bool:
            return backing.exists(node_id)

        def get(self, node_id: str) -> Node:
            return backing.get(node_id)

        def put(self, node: Node) -> None:
            backing.put(node)
            if invocation == 1:
                raise OSError("injected destination body failure")

        def update_meta(self, node_id: str, patch: dict[str, object]) -> None:
            backing.update_meta(node_id, patch)

    real_open_store = cli_module._open_store

    @contextmanager
    def partial_open_store(
        spec: str,
        *,
        create: bool,
        initialize_if_missing: bool = False,
    ) -> Iterator[object]:
        if create:
            yield PartialDestination()
            return
        with real_open_store(
            spec,
            create=create,
            initialize_if_missing=initialize_if_missing,
        ) as store:
            yield store

    def new_directory() -> Path:
        directory = tmp_path / f"partial-spool-{len(directories)}"
        directory.mkdir(mode=0o700)
        directories.append(directory)
        return directory

    monkeypatch.setattr(cli_module, "_open_store", partial_open_store)
    monkeypatch.setattr(cli_module, "_new_merge_spool_directory", new_directory)
    arguments = ["merge", str(tmp_path / "logical-destination.db"), str(source), "--json"]

    invocation = 1
    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "destination body failure" in captured.err
    assert backing.exists(root_id)

    invocation = 2
    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["copied"] == 1
    assert second["existing"] == 1

    invocation = 3
    snapshot = [backing.get(node.id) for node in backing.walk(root_id)]
    assert main(arguments) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["copied"] == 0
    assert third["existing"] == 2
    assert [backing.get(node.id) for node in backing.walk(root_id)] == snapshot
    assert all(not directory.exists() for directory in directories)


def test_cli_pg_env_requires_configured_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MISSING", raising=False)
    assert main(["runs", "pg-env:MISSING#team", "--json"]) == 2
    assert "MISSING" in capsys.readouterr().err


def test_cli_read_commands_accept_pg_env_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    dsn = (
        "postgresql://private-user:private-password@database.example/private-db"
        "?sslmode=require"
    )
    spec = "pg-env:POLLARD_PG_DSN#team"
    backing = MemoryStore()
    run = Runtime(backing).run("postgres-cli")
    run.model_call(
        {"model": "mock-1", "prompt": "inspect this run"},
        fn=lambda _payload: {
            "text": "stored result",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    root_id = run.root_id
    constructed: list[tuple[str, str]] = []
    entered: list[str] = []
    exited: list[str] = []

    class FakePostgresStore:
        def __init__(self, conninfo: str, *, store_id: str = "default") -> None:
            constructed.append((conninfo, store_id))
            self.store_id = store_id

        def __enter__(self) -> MemoryStore:
            entered.append(self.store_id)
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append(self.store_id)

    monkeypatch.setenv("POLLARD_PG_DSN", dsn)
    monkeypatch.setattr("pollard.cli.PostgresStore", FakePostgresStore)
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    shown = json.loads(captured.out)
    assert shown["root_id"] == root_id
    assert len(shown["nodes"]) == 2

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    reported = json.loads(captured.out)
    assert reported["root_id"] == root_id
    assert reported["nodes"] == 2
    assert reported["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]
    assert verified["nodes"] == 2

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)
    assert sealed["root_id"] == root_id
    assert len(sealed["digest"]) == 64

    export_path = tmp_path / "postgres-subtree.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    manifest_text = export_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert exported == {
        "digest": sealed["digest"],
        "nodes": 2,
        "path": str(export_path),
        "root_id": root_id,
    }
    assert manifest["root_id"] == root_id
    assert manifest["seal"]["digest"] == sealed["digest"]

    assert constructed == [(dsn, "team")] * 5
    assert entered == ["team"] * 5
    assert exited == ["team"] * 5
    exposed = "".join(transcript) + manifest_text
    for secret in (dsn, "private-user", "private-password"):
        assert secret not in exposed


def test_cli_pg_env_psycopg_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = "postgresql://private-user:private-password@database.example/private-db"
    spec = "pg-env:POLLARD_PG_DSN#team"
    psycopg_failure = type(
        "OperationalError",
        (Exception,),
        {"__module__": "psycopg.errors"},
    )
    exited: list[bool] = []

    class FailingStore:
        def get(self, _node_id: str) -> Node:
            raise psycopg_failure(f"connection failed for {dsn}")

    class FakePostgresStore:
        def __init__(self, conninfo: str, *, store_id: str = "default") -> None:
            assert conninfo == dsn
            assert store_id == "team"

        def __enter__(self) -> FailingStore:
            return FailingStore()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type is psycopg_failure)

    monkeypatch.setenv("POLLARD_PG_DSN", dsn)
    monkeypatch.setattr("pollard.cli.PostgresStore", FakePostgresStore)

    assert main(["show", spec, "missing-root", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not access pg-env:POLLARD_PG_DSN#team" in captured.err
    assert exited == [True]
    for secret in (dsn, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_redis_env_requires_configured_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MISSING", raising=False)
    assert main(["runs", "redis-env:MISSING", "--json"]) == 2
    assert "MISSING" in capsys.readouterr().err


def test_cli_redis_missing_variable_name_cannot_split_error_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    variable = "MISSING\u2028FORGED"
    monkeypatch.delenv(variable, raising=False)

    assert main(["runs", f"redis-env:{variable}", "--json"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "\u2028" not in captured.err
    assert "MISSING%E2%80%A8FORGED" in captured.err


def test_cli_redis_env_defaults_prefix_and_store_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    backing = MemoryStore()
    run = Runtime(backing).run("redis-defaults")
    root_id = run.root_id
    constructed: list[tuple[str, str, str]] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_url, store_id, prefix))

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert main(["show", "redis-env:POLLARD_REDIS_URL", root_id, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["root_id"] == root_id
    assert constructed == [(url, "default", "pollard")]
    assert url not in captured.out + captured.err


def test_cli_read_commands_accept_redis_env_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "rediss://private-user:private-password@redis.example/0"
    prefix = "tenant-cli"
    store_id = "team"
    spec = f"redis-env:POLLARD_REDIS_URL?prefix={prefix}#{store_id}"
    backing = MemoryStore()
    run = Runtime(backing).run("redis-cli")
    run.model_call(
        {"model": "mock-1", "prompt": "inspect this run"},
        fn=lambda _payload: {
            "text": "stored result",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    root_id = run.root_id
    constructed: list[tuple[str, str, str]] = []
    entered: list[tuple[str, str]] = []
    exited: list[tuple[str, str]] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_url, store_id, prefix))
            self.store_id = store_id
            self.prefix = prefix

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            entered.append((self.store_id, self.prefix))
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append((self.store_id, self.prefix))

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    shown = json.loads(captured.out)
    assert shown["root_id"] == root_id
    assert len(shown["nodes"]) == 2

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    reported = json.loads(captured.out)
    assert reported["root_id"] == root_id
    assert reported["nodes"] == 2
    assert reported["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]
    assert verified["nodes"] == 2

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)
    assert sealed["root_id"] == root_id
    assert len(sealed["digest"]) == 64

    export_path = tmp_path / "redis-subtree.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    manifest_text = export_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert exported == {
        "digest": sealed["digest"],
        "nodes": 2,
        "path": str(export_path),
        "root_id": root_id,
    }
    assert manifest["root_id"] == root_id
    assert manifest["seal"]["digest"] == sealed["digest"]

    expected_configuration = (url, store_id, prefix)
    assert constructed == [expected_configuration] * 5
    assert entered == [(store_id, prefix)] * 5
    assert exited == [(store_id, prefix)] * 5
    exposed = "".join(transcript) + manifest_text
    for secret in (url, "private-user", "private-password"):
        assert secret not in exposed


def test_cli_runs_and_merge_accept_redis_env_as_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    spec = "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team"
    label = "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team"
    backing = MemoryStore()
    run = Runtime(backing).run("redis-source")
    run.note({"label": "merge-me"})
    root_id = run.root_id
    constructed: list[tuple[str, str, str]] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_url, store_id, prefix))

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)
    transcript: list[str] = []

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    listed = json.loads(captured.out)
    assert listed["runs"] == [
        {
            "attempt": 0,
            "label": "redis-source",
            "nodes": 2,
            "pruned": 0,
            "root_id": root_id,
            "store": label,
        }
    ]

    destination = tmp_path / "redis-merged.db"
    assert main(["merge", str(destination), spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    merged = json.loads(captured.out)
    assert merged["copied"] == 2
    assert merged["sources"][0]["source"] == label
    with SQLiteStore(destination) as store:
        assert store.exists(root_id)
        assert len(list(store.walk(root_id))) == 2

    assert constructed == [(url, "team", "tenant-cli")] * 2
    exposed = "".join(transcript)
    for secret in (url, "private-user", "private-password"):
        assert secret not in exposed


def test_cli_direct_redis_url_strips_fragment_and_sanitizes_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_url = (
        "redis://private-user:private-password@redis.example/0"
        "?client_name=pollard"
    )
    spec = f"{connection_url}#direct-team"
    backing = MemoryStore()
    run = Runtime(backing).run("direct-redis")
    constructed: list[tuple[str, str, str]] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_url, store_id, prefix))

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert constructed == [(connection_url, "direct-team", "pollard")]
    assert listed["runs"][0]["root_id"] == run.root_id
    assert listed["runs"][0]["store"] == "redis://redis.example#direct-team"
    exposed = captured.out + captured.err
    for secret in (spec, connection_url, "private-user", "private-password"):
        assert secret not in exposed


@pytest.mark.parametrize("scheme", ["REDIS", "REDISS"])
def test_cli_routes_uppercase_redis_schemes(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_url = (
        f"{scheme}://private-user:private-password@redis.example/0"
    )
    spec = f"{connection_url}#team"
    backing = MemoryStore()
    run = Runtime(backing).run(f"{scheme.lower()}-uppercase")
    constructed: list[tuple[str, str, str]] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_url, store_id, prefix))

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert len(constructed) == 1
    configured_url, store_id, prefix = constructed[0]
    assert configured_url.lower() == connection_url.lower()
    assert (store_id, prefix) == ("team", "pollard")
    assert listed["runs"][0]["root_id"] == run.root_id
    assert (
        listed["runs"][0]["store"].lower()
        == f"{scheme.lower()}://redis.example#team"
    )
    for secret in (spec, connection_url, "private-user", "private-password"):
        assert secret not in captured.out + captured.err


@pytest.mark.parametrize("indirect", [True, False], ids=["env", "direct"])
def test_cli_sanitizes_redis_urls_with_credential_adjacent_invalid_ports(
    indirect: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port_secret = "private-port-secret"
    url = (
        "redis://private-user:private-password@redis.example:"
        f"{port_secret}/0"
    )
    spec = (
        "redis-env:POLLARD_BAD_REDIS_URL#team"
        if indirect
        else f"{url}#team"
    )

    class InvalidPortRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_url == url
            assert (store_id, prefix) == ("team", "pollard")
            raise ValueError(f"invalid port {port_secret} in {configured_url}")

    monkeypatch.setenv("POLLARD_BAD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", InvalidPortRedisStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for secret in (
        url,
        "private-user",
        "private-password",
        port_secret,
    ):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("spec", "control", "encoded"),
    [
        (
            "redis-env:POLLARD_REDIS_URL?prefix=tenant%0AFORGED#team",
            "\nFORGED",
            "%0A",
        ),
        (
            "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team\x1b[31mFORGED",
            "\x1b",
            "%1B",
        ),
    ],
    ids=["prefix", "store-id"],
)
def test_cli_redis_labels_encode_or_reject_control_values(
    spec: str,
    control: str,
    encoded: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    backing = MemoryStore()
    Runtime(backing).run("hostile-label")

    class FakeRedisStore:
        def __init__(
            self,
            _configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert store_id
            assert prefix

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    result = main(["runs", spec, spec])
    captured = capsys.readouterr()
    transcript = captured.out + captured.err
    assert control not in transcript
    if result == 0:
        assert encoded in captured.out.upper()
    else:
        assert result == 2
        assert "redis" in captured.err.lower()
    for secret in (url, "private-user", "private-password"):
        assert secret not in transcript


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("?database=0", "accepts only one prefix parameter"),
        ("?prefix=one&prefix=two", "accepts only one prefix parameter"),
        ("?prefix=", "prefix must be a non-empty string"),
        ("?prefix=tenant%ZZ", "invalid redis-env store spec"),
    ],
)
def test_cli_rejects_invalid_redis_env_prefix_queries(
    query: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "redis://private-user:private-password@redis.example/0"

    def unexpected_redis(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Redis spec was opened")

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", unexpected_redis)

    spec = f"redis-env:POLLARD_REDIS_URL{query}#team"
    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    for secret in (url, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_redis_module_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    prefix = "tenant-cli"
    store_id = "team"
    spec = f"redis-env:POLLARD_REDIS_URL?prefix={prefix}#{store_id}"
    redis_failure = type(
        "ConnectionError",
        (Exception,),
        {"__module__": "redis.exceptions"},
    )
    exited: list[bool] = []

    class FailingStore:
        def get(self, _node_id: str) -> Node:
            raise redis_failure(f"connection failed for {url}")

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_url == url
            assert store_id == "team"
            assert prefix == "tenant-cli"

        def __getattr__(self, name: str) -> object:
            return getattr(FailingStore(), name)

        def __enter__(self) -> FailingStore:
            return FailingStore()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type is redis_failure)

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert main(["show", spec, "missing-root", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {spec}" in captured.err
    assert exited == [True]
    for secret in (url, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_merge_accepts_redis_env_destination_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "rediss://private-user:private-password@redis.example/0"
    destination = "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team"
    source = tmp_path / "source.db"
    root_id, _payload = _recording(source)
    backing = MemoryStore()
    constructed: list[tuple[str, str, str, bool]] = []
    exited: list[bool] = []

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            constructed.append((configured_url, store_id, prefix, create))

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append(True)

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)
    transcript: list[str] = []

    merge_args = [
        "merge",
        destination,
        str(source),
        "--initialize-if-missing",
        "--json",
    ]
    assert main(merge_args) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    first = json.loads(captured.out)
    assert first == {
        "copied": 2,
        "destination": destination,
        "existing": 0,
        "meta_conflicts": 0,
        "result_conflicts": 0,
        "sources": [
            {
                "copied": 2,
                "existing": 0,
                "meta_conflicts": 0,
                "result_conflicts": 0,
                "source": str(source),
            }
        ],
    }

    assert main(["merge", destination, str(source), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    second = json.loads(captured.out)
    assert second["copied"] == 0
    assert second["existing"] == 2
    assert second["result_conflicts"] == 0
    assert second["meta_conflicts"] == 0
    assert second["sources"][0]["copied"] == 0
    assert second["sources"][0]["existing"] == 2

    assert constructed == [
        (url, "team", "tenant-cli", True),
        (url, "team", "tenant-cli", False),
    ]
    assert exited == [True, True]
    assert len(list(backing.walk(root_id))) == 2
    exposed = "".join(transcript)
    for secret in (url, "private-user", "private-password"):
        assert secret not in exposed


def test_cli_merge_into_existing_redis_destination_records_conflicts_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    destination = "redis-env:POLLARD_REDIS_URL?prefix=existing#team"
    source_path = tmp_path / "conflicting-source.db"
    backing = MemoryStore()
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "redis-existing"},
    )
    left = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "left"},
        meta={"worker": "left", "nested": {"a": 1}, "tags": ["left"]},
    )
    right = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "right"},
        meta={"worker": "right", "nested": {"b": 2}, "tags": ["right"]},
    )
    added = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"label": "new"},
    )
    backing.put(root)
    backing.put(left)
    with SQLiteStore(source_path) as source:
        source.put(root)
        source.put(right)
        source.put(added)

    class FakeRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert configured_url == url
            assert (store_id, prefix, create) == ("team", "existing", False)

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["copied"] == 1
    assert first["existing"] == 2
    assert first["result_conflicts"] == 1
    assert first["meta_conflicts"] == 1
    stored = backing.get(left.id)
    assert stored.result == {"text": "left"}
    assert stored.meta["nested"] == {"a": 1, "b": 2}
    assert stored.meta["tags"] == ["left", "right"]
    assert stored.meta["merge_conflicts"] == [
        {"path": "worker", "values": ["left", "right"]}
    ]
    assert stored.meta["result_conflicts"][0]["result"] == {"text": "right"}
    snapshot = list(backing.walk(root.id))

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 3
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert list(backing.walk(root.id)) == snapshot


@pytest.mark.parametrize("scheme", ["redis", "rediss", "REDIS", "REDISS"])
def test_cli_rejects_direct_redis_url_as_merge_destination_without_connecting(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    connection_url = (
        f"{scheme}://private-user:private-password@redis.example/0"
    )
    destination = f"{connection_url}#team"
    source = tmp_path / "source.db"
    _recording(source)

    def unexpected_redis(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct Redis destination was opened")

    monkeypatch.setattr("pollard.cli.RedisStore", unexpected_redis)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "redis" in captured.err.lower()
    assert "destination" in captured.err.lower()
    assert "redis-env:" in captured.err
    for secret in (destination, connection_url, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "destination",
    [
        "redis-env:POLLARD_REDIS_URL#team",
        "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli",
        "redis-env:POLLARD_REDIS_URL",
    ],
    ids=["missing-prefix", "missing-store-id", "missing-both"],
)
def test_cli_redis_destination_requires_explicit_namespace_before_access(
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"

    def unexpected_redis(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("incomplete Redis destination was opened")

    monkeypatch.delenv("POLLARD_REDIS_URL", raising=False)
    monkeypatch.setattr("pollard.cli.RedisStore", unexpected_redis)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "explicit prefix and store id" in captured.err
    assert not source.exists()


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        (
            "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team%ZZ",
            "invalid redis-env store spec",
        ),
        (
            "redis-env:POLLARD_REDIS_URL?prefix=tenant%20cli#team",
            "must not contain whitespace",
        ),
        (
            "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team%20one",
            "must not contain whitespace",
        ),
    ],
    ids=["bad-escape", "prefix-whitespace", "store-id-whitespace"],
)
def test_cli_redis_destination_rejects_ambiguous_namespace_before_access(
    destination: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"

    def unexpected_redis(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ambiguous Redis destination was opened")

    monkeypatch.delenv("POLLARD_REDIS_URL", raising=False)
    monkeypatch.setattr("pollard.cli.RedisStore", unexpected_redis)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not source.exists()


@pytest.mark.parametrize(
    "destination_kind",
    [
        "sqlite",
        "postgres",
        "direct-redis",
        "direct-mongo",
        "direct-neo4j",
        "kafka-env",
    ],
)
def test_cli_initialize_if_missing_requires_supported_env_destination(
    destination_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    _recording(source)
    sqlite_destination = tmp_path / "destination.db"
    direct_url = "redis://private-user:private-password@redis.example/0#team"
    direct_mongo = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    direct_neo4j = (
        "neo4j+s://private-user:private-password@graph.example/"
    )
    destinations = {
        "sqlite": str(sqlite_destination),
        "postgres": "pg-env:UNSET_POSTGRES_DSN#team",
        "direct-redis": direct_url,
        "direct-mongo": direct_mongo,
        "direct-neo4j": direct_neo4j,
        "kafka-env": (
            "kafka-env:UNSET_KAFKA_CONFIG?topic=tenant.audit#team"
        ),
    }

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid initialization destination was opened")

    monkeypatch.delenv("UNSET_POSTGRES_DSN", raising=False)
    monkeypatch.setattr("pollard.cli.PostgresStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.RedisStore", unexpected_store)

    assert (
        main(
            [
                "merge",
                destinations[destination_kind],
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--initialize-if-missing" in captured.err
    assert "redis-env:" in captured.err
    assert not sqlite_destination.exists()
    for secret in (
        direct_url,
        direct_mongo,
        direct_neo4j,
        "private-user",
        "private-password",
    ):
        assert secret not in captured.err


def test_cli_preflights_sources_before_creating_redis_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "redis://private-user:private-password@redis.example/0"
    destination = "redis-env:POLLARD_REDIS_URL?prefix=preflight#team"
    source = tmp_path / "source.db"
    missing = tmp_path / "missing-source.db"
    _recording(source)
    backing = MemoryStore()
    constructed: list[bool] = []

    class FakeRedisStore:
        def __init__(
            self,
            _configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert (store_id, prefix) == ("team", "preflight")
            constructed.append(create)

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FakeRedisStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                str(missing),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing-source.db" in captured.err
    assert constructed == []
    assert backing.roots() == []
    for secret in (url, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_redis_source_read_failure_is_attributed_before_destination_opens(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_url = "redis://source-user:source-password@source.example/0"
    destination_url = (
        "rediss://destination-user:destination-password@destination.example/0"
    )
    source = "redis-env:POLLARD_SOURCE_REDIS?prefix=source-prefix#source-store"
    destination = (
        "redis-env:POLLARD_DESTINATION_REDIS"
        "?prefix=destination-prefix#destination-store"
    )
    redis_failure = type(
        "ConnectionError",
        (Exception,),
        {"__module__": "redis.exceptions"},
    )
    constructed: list[tuple[str, bool]] = []

    class FailingSourceRedisStore:
        def __init__(
            self,
            _configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert prefix
            constructed.append((store_id, create))

        def __enter__(self) -> "FailingSourceRedisStore":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def roots(self) -> list[str]:
            raise redis_failure(f"source read failed for {source_url}")

    monkeypatch.setenv("POLLARD_SOURCE_REDIS", source_url)
    monkeypatch.setenv("POLLARD_DESTINATION_REDIS", destination_url)
    monkeypatch.setattr("pollard.cli.RedisStore", FailingSourceRedisStore)

    assert (
        main(
            [
                "merge",
                destination,
                source,
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "could not access "
        "redis-env:POLLARD_SOURCE_REDIS"
        "?prefix=source-prefix#source-store"
        in captured.err
    )
    assert destination not in captured.err
    assert constructed == [("source-store", False)]
    for secret in (
        source_url,
        destination_url,
        "source-user",
        "source-password",
        "destination-user",
        "destination-password",
    ):
        assert secret not in captured.err


def test_cli_redis_destination_connection_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = "rediss://private-user:private-password@redis.example/0"
    destination = "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team"
    source = tmp_path / "source.db"
    _recording(source)
    redis_failure = type(
        "ConnectionError",
        (Exception,),
        {"__module__": "redis.exceptions"},
    )

    class FailingRedisStore:
        def __init__(
            self,
            configured_url: str,
            *,
            store_id: str = "default",
            prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert configured_url == url
            assert (store_id, prefix, create) == ("team", "tenant-cli", True)
            raise redis_failure(f"could not connect to {configured_url}")

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr("pollard.cli.RedisStore", FailingRedisStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {destination}" in captured.err
    for secret in (url, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "url_query",
    [
        "decode_responses=false",
        "encoding=utf-16",
        "encoding_errors=ignore",
    ],
)
def test_cli_redis_destination_rejects_text_overrides_credential_safely(
    url_query: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = (
        "rediss://private-user:private-password@redis.example/0"
        f"?{url_query}"
    )
    destination = "redis-env:POLLARD_REDIS_URL?prefix=tenant-cli#team"
    source = tmp_path / "source.db"
    _recording(source)
    imported: list[str] = []

    def unexpected_import(name: str) -> object:
        imported.append(name)
        raise AssertionError("Redis client module was imported")

    monkeypatch.setenv("POLLARD_REDIS_URL", url)
    monkeypatch.setattr(
        "pollard.stores.redis.import_module",
        unexpected_import,
    )

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {destination}" in captured.err
    assert imported == []
    for secret in (url, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("variable", "shown"),
    [
        ("MISSING_MONGODB_URI", "MISSING_MONGODB_URI"),
        ("MISSING\u2028FORGED", "MISSING%E2%80%A8FORGED"),
    ],
)
def test_cli_mongo_env_requires_configured_variable_without_line_injection(
    variable: str,
    shown: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(variable, raising=False)

    assert main(["runs", f"mongo-env:{variable}", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert shown in captured.err
    assert "\u2028" not in captured.err
    assert len(captured.err.splitlines()) == 1


def test_cli_mongo_env_defaults_database_prefix_and_store_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    spec = "mongo-env:POLLARD_MONGODB_URI"
    backing = MemoryStore()
    run = Runtime(backing).run("mongo-defaults")
    constructed: list[tuple[str, str, str, str]] = []

    class FakeMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append(
                (configured_uri, database, store_id, collection_prefix)
            )

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == run.root_id
    assert (
        listed["runs"][0]["store"]
        == "mongo-env:POLLARD_MONGODB_URI"
        "?database=pollard&prefix=pollard#default"
    )
    assert constructed == [(uri, "pollard", "default", "pollard")]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.out + captured.err


def test_cli_observation_commands_accept_mongo_env_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    database = "tenant_db"
    collection_prefix = "pollard_cli"
    store_id = "team"
    spec = (
        "mongo-env:POLLARD_MONGODB_URI"
        f"?database={database}&prefix={collection_prefix}#{store_id}"
    )
    backing = MemoryStore()
    run = Runtime(backing).run("mongo-cli")
    run.model_call(
        {"model": "mock-1", "prompt": "inspect this run"},
        fn=lambda _payload: {
            "text": "stored result",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    root_id = run.root_id
    constructed: list[tuple[str, str, str, str]] = []
    entered: list[tuple[str, str, str]] = []
    exited: list[tuple[str, str, str]] = []

    class FakeMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append(
                (configured_uri, database, store_id, collection_prefix)
            )
            self.database = database
            self.store_id = store_id
            self.collection_prefix = collection_prefix

        def __enter__(self) -> MemoryStore:
            entered.append(
                (self.database, self.store_id, self.collection_prefix)
            )
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append(
                (self.database, self.store_id, self.collection_prefix)
            )

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    shown = json.loads(captured.out)
    assert shown["root_id"] == root_id
    assert len(shown["nodes"]) == 2

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    reported = json.loads(captured.out)
    assert reported["root_id"] == root_id
    assert reported["nodes"] == 2
    assert reported["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]
    assert verified["nodes"] == 2

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)
    assert sealed["root_id"] == root_id
    assert len(sealed["digest"]) == 64

    export_path = tmp_path / "mongo-subtree.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    manifest_text = export_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert exported == {
        "digest": sealed["digest"],
        "nodes": 2,
        "path": str(export_path),
        "root_id": root_id,
    }
    assert manifest["root_id"] == root_id
    assert manifest["seal"]["digest"] == sealed["digest"]

    expected = (uri, database, store_id, collection_prefix)
    assert constructed == [expected] * 5
    namespace = (database, store_id, collection_prefix)
    assert entered == [namespace] * 5
    assert exited == [namespace] * 5
    exposed = "".join(transcript) + manifest_text
    for secret in (uri, "private-user", "private-password"):
        assert secret not in exposed


def test_cli_runs_and_merge_accept_mongo_env_as_source_with_canonical_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    spec = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?prefix=pollard_cli&database=tenant_db#team"
    )
    label = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant_db&prefix=pollard_cli#team"
    )
    backing = MemoryStore()
    run = Runtime(backing).run("mongo-source")
    run.note({"label": "merge-me"})
    root_id = run.root_id
    constructed: list[tuple[str, str, str, str]] = []

    class FakeMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append(
                (configured_uri, database, store_id, collection_prefix)
            )

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)
    transcript: list[str] = []

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == root_id
    assert listed["runs"][0]["store"] == label

    destination = tmp_path / "mongo-merged.db"
    assert main(["merge", str(destination), spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    merged = json.loads(captured.out)
    assert merged["copied"] == 2
    assert merged["sources"][0]["source"] == label
    with SQLiteStore(destination) as store:
        assert store.exists(root_id)
        assert len(list(store.walk(root_id))) == 2

    assert constructed == [(uri, "tenant_db", "team", "pollard_cli")] * 2
    exposed = "".join(transcript)
    for secret in (uri, "private-user", "private-password"):
        assert secret not in exposed


@pytest.mark.parametrize(
    ("suffix", "message"),
    [
        (
            "?unknown=value#team",
            "accepts only database and prefix parameters",
        ),
        (
            "?database=one&database=two#team",
            "accepts each namespace parameter once",
        ),
        (
            "?prefix=one&prefix=two#team",
            "accepts each namespace parameter once",
        ),
        ("?database=#team", "database must be a non-empty string"),
        ("?prefix=#team", "prefix must be a non-empty string"),
        ("?prefix=bad-prefix#team", "prefix must start with a letter"),
        ("?database#team", "store spec has an invalid query"),
        ("#team?database=tenant_db", "query must precede"),
    ],
)
def test_cli_rejects_invalid_mongo_env_namespace_queries(
    suffix: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "mongodb://mongo.example/?replicaSet=rs0"

    def unexpected_mongo(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid MongoDB spec was opened")

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", unexpected_mongo)

    assert (
        main(
            [
                "runs",
                f"mongo-env:POLLARD_MONGODB_URI{suffix}",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert uri not in captured.err


@pytest.mark.parametrize(
    ("spec", "control"),
    [
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant%0AFORGED&prefix=pollard#team",
            "\nFORGED",
        ),
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant&prefix=pollard%0AFORGED#team",
            "\nFORGED",
        ),
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant&prefix=pollard#team\x1b[31mFORGED",
            "\x1b",
        ),
    ],
    ids=["database", "prefix", "store-id"],
)
def test_cli_rejects_control_characters_in_mongo_namespaces(
    spec: str,
    control: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )

    def unexpected_mongo(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("hostile MongoDB spec was opened")

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", unexpected_mongo)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    transcript = captured.out + captured.err
    assert captured.out == ""
    assert control not in transcript
    assert "control characters" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in transcript


def test_cli_mongo_constructor_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    spec = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant_db&prefix=pollard_cli#team"
    )

    class FailingMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert (database, store_id, collection_prefix) == (
                "tenant_db",
                "team",
                "pollard_cli",
            )
            raise ValueError(f"could not parse MongoDB URI {configured_uri}")

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FailingMongoStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {spec}" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_pymongo_body_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    spec = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant_db&prefix=pollard_cli#team"
    )
    pymongo_failure = type(
        "ServerSelectionTimeoutError",
        (Exception,),
        {"__module__": "pymongo.errors"},
    )
    exited: list[bool] = []

    class FailingStore:
        def get(self, _node_id: str) -> Node:
            raise pymongo_failure(f"connection failed for {uri}")

    class FakeMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert (database, store_id, collection_prefix) == (
                "tenant_db",
                "team",
                "pollard_cli",
            )

        def __enter__(self) -> FailingStore:
            return FailingStore()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type is pymongo_failure)

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)

    assert main(["show", spec, "missing-root", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {spec}" in captured.err
    assert exited == [True]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_mongo_constructor_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    spec = "mongo-env:POLLARD_MONGODB_URI#team"
    closed: list[bool] = []

    class WarningMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert (database, store_id, collection_prefix) == (
                "pollard",
                "team",
                "pollard",
            )
            warnings.warn(
                f"MongoDB warning exposed {configured_uri}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

        def __enter__(self) -> object:
            raise AssertionError("warning-producing MongoStore was entered")

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", WarningMongoStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not access" in captured.err
    assert closed == [True]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_mongo_destination_missing_environment_after_source_preflight(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "mongo-env:UNSET_MONGODB_URI"
        "?database=tenant_db&prefix=pollard_cli#team"
    )
    source = tmp_path / "source.db"
    _recording(source)

    def unexpected_mongo(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("MongoDB destination was opened")

    monkeypatch.delenv("UNSET_MONGODB_URI", raising=False)
    monkeypatch.setattr("pollard.cli.MongoStore", unexpected_mongo)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "MongoDB URI environment variable is not set" in captured.err
    assert "UNSET_MONGODB_URI" in captured.err


def test_cli_merge_accepts_mongo_env_destination_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = (
        "mongodb://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant_db&prefix=pollard_cli#team"
    )
    source = tmp_path / "mongo-destination-source.db"
    root_id, _payload = _recording(source)
    backing = MemoryStore()
    constructed: list[tuple[str, str, str, str, bool]] = []

    class FakeMongoStore:
        def __init__(
            self,
            configured_uri: str,
            *,
            database: str = "pollard",
            store_id: str = "default",
            collection_prefix: str = "pollard",
            create: bool = True,
        ) -> None:
            constructed.append(
                (
                    configured_uri,
                    database,
                    store_id,
                    collection_prefix,
                    create,
                )
            )

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)
    transcript: list[str] = []

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    first = json.loads(captured.out)
    assert first["copied"] == 2
    assert first["existing"] == 0

    assert main(["merge", destination, str(source), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    repeated = json.loads(captured.out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert len(list(backing.walk(root_id))) == 2
    assert constructed == [
        (uri, "tenant_db", "team", "pollard_cli", True),
        (uri, "tenant_db", "team", "pollard_cli", False),
    ]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in "".join(transcript)


def test_cli_merge_into_existing_mongo_destination_records_conflicts_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "mongodb://private-user:private-password@mongo.example/?replicaSet=rs0"
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant&prefix=existing#team"
    )
    source_path = tmp_path / "mongo-conflicting-source.db"
    backing = MemoryStore()
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "mongo-existing"},
    )
    left = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "left"},
        meta={"worker": "left", "nested": {"a": 1}},
    )
    right = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "right"},
        meta={"worker": "right", "nested": {"b": 2}},
    )
    backing.put(root)
    backing.put(left)
    with SQLiteStore(source_path) as source:
        source.put(root)
        source.put(right)

    class FakeMongoStore:
        def __init__(self, configured_uri: str, **options: object) -> None:
            assert configured_uri == uri
            assert options == {
                "database": "tenant",
                "store_id": "team",
                "collection_prefix": "existing",
                "create": False,
            }

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["existing"] == 2
    assert first["result_conflicts"] == 1
    assert first["meta_conflicts"] == 1
    snapshot = list(backing.walk(root.id))

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert list(backing.walk(root.id)) == snapshot


@pytest.mark.parametrize(
    "scheme",
    ["mongodb", "mongodb+srv", "MONGODB", "MONGODB+SRV"],
)
def test_cli_rejects_direct_mongo_uri_as_merge_destination_without_connecting(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        f"{scheme}://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )
    source = tmp_path / "mongo-direct-source.db"
    _recording(source)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct MongoDB destination was opened")

    monkeypatch.setattr("pollard.cli.MongoStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct MongoDB URI" in captured.err
    assert "mongo-env:" in captured.err
    for secret in (destination, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "destination",
    [
        "mongo-env:POLLARD_MONGODB_URI?prefix=pollard_cli#team",
        "mongo-env:POLLARD_MONGODB_URI?database=tenant#team",
        "mongo-env:POLLARD_MONGODB_URI?database=tenant&prefix=pollard_cli",
        "mongo-env:POLLARD_MONGODB_URI#team",
    ],
    ids=["missing-database", "missing-prefix", "missing-store-id", "missing-query"],
)
def test_cli_mongo_destination_requires_explicit_namespace_before_access(
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"
    monkeypatch.delenv("POLLARD_MONGODB_URI", raising=False)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "explicit database, prefix, and store id" in captured.err
    assert not source.exists()


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant&prefix=pollard_cli#team%ZZ",
            "invalid mongo-env store spec",
        ),
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant%20db&prefix=pollard_cli#team",
            "must not contain whitespace",
        ),
        (
            "mongo-env:POLLARD_MONGODB_URI"
            "?database=tenant&prefix=pollard_cli#team%20one",
            "must not contain whitespace",
        ),
    ],
)
def test_cli_mongo_destination_rejects_ambiguous_namespace_before_access(
    destination: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"
    monkeypatch.delenv("POLLARD_MONGODB_URI", raising=False)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not source.exists()


def test_cli_preflights_sources_before_creating_mongo_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "mongodb://private-user:private-password@mongo.example/?replicaSet=rs0"
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant&prefix=preflight#team"
    )
    source = tmp_path / "mongo-preflight-source.db"
    missing = tmp_path / "mongo-missing-source.db"
    _recording(source)
    constructed: list[bool] = []

    class FakeMongoStore:
        def __init__(self, _uri: str, **options: object) -> None:
            constructed.append(bool(options["create"]))

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                str(missing),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mongo-missing-source.db" in captured.err
    assert constructed == []
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_mongo_source_failure_prevents_mongo_destination_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_uri = (
        "mongodb://source-user:source-password@source.example/?replicaSet=rs0"
    )
    destination_uri = (
        "mongodb://destination-user:destination-password@destination.example/"
        "?replicaSet=rs0"
    )
    source = (
        "mongo-env:POLLARD_SOURCE_MONGODB"
        "?database=source_db&prefix=source_prefix#source-store"
    )
    destination = (
        "mongo-env:POLLARD_DESTINATION_MONGODB"
        "?database=destination_db&prefix=destination_prefix#destination-store"
    )
    pymongo_failure = type(
        "NetworkTimeout",
        (Exception,),
        {"__module__": "pymongo.errors"},
    )
    constructed: list[tuple[str, bool]] = []

    class FailingSourceMongoStore:
        def __init__(
            self,
            configured_uri: str,
            **options: object,
        ) -> None:
            constructed.append((configured_uri, bool(options["create"])))
            assert configured_uri == source_uri

        def __enter__(self) -> "FailingSourceMongoStore":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def roots(self) -> list[str]:
            raise pymongo_failure(f"source traversal failed for {source_uri}")

    monkeypatch.setenv("POLLARD_SOURCE_MONGODB", source_uri)
    monkeypatch.setenv("POLLARD_DESTINATION_MONGODB", destination_uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FailingSourceMongoStore)

    assert (
        main(
            [
                "merge",
                destination,
                source,
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {source}" in captured.err
    assert destination not in captured.err
    assert constructed == [(source_uri, False)]
    for secret in (
        source_uri,
        destination_uri,
        "source-user",
        "source-password",
        "destination-user",
        "destination-password",
    ):
        assert secret not in captured.err


def test_cli_mongo_destination_constructor_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "mongodb://private-user:private-password@mongo.example/?replicaSet=rs0"
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant&prefix=destination#team"
    )
    source = tmp_path / "mongo-constructor-source.db"
    _recording(source)

    class FailingMongoStore:
        def __init__(self, configured_uri: str, **options: object) -> None:
            assert configured_uri == uri
            assert options["create"] is True
            raise ValueError(f"failed MongoDB connection to {configured_uri}")

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FailingMongoStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {destination}" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_mongo_destination_warning_closes_store_credential_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "mongodb://private-user:private-password@mongo.example/?replicaSet=rs0"
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant&prefix=destination#team"
    )
    source = tmp_path / "mongo-warning-source.db"
    _recording(source)
    closed: list[bool] = []

    class WarningMongoStore:
        def __init__(self, configured_uri: str, **options: object) -> None:
            assert configured_uri == uri
            assert options["create"] is False
            warnings.warn(
                f"MongoDB warning exposed {configured_uri}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", WarningMongoStore)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {destination}" in captured.err
    assert closed == [True]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_mongo_destination_driver_write_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "mongodb://private-user:private-password@mongo.example/?replicaSet=rs0"
    destination = (
        "mongo-env:POLLARD_MONGODB_URI"
        "?database=tenant&prefix=destination#team"
    )
    source = tmp_path / "mongo-write-source.db"
    _recording(source)
    pymongo_failure = type(
        "OperationFailure",
        (Exception,),
        {"__module__": "pymongo.errors"},
    )
    exited: list[type[BaseException] | None] = []

    class FailingDestination:
        def exists(self, _node_id: str) -> bool:
            return False

        def put(self, _node: Node) -> None:
            raise pymongo_failure(f"write failed for {uri}")

    class FakeMongoStore:
        def __init__(self, configured_uri: str, **_options: object) -> None:
            assert configured_uri == uri

        def __enter__(self) -> FailingDestination:
            return FailingDestination()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type)

    monkeypatch.setenv("POLLARD_MONGODB_URI", uri)
    monkeypatch.setattr("pollard.cli.MongoStore", FakeMongoStore)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {destination}" in captured.err
    assert exited == [pymongo_failure]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize("scheme", ["mongodb", "mongodb+srv"])
def test_cli_rejects_direct_mongo_uri_without_sqlite_fallback(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = (
        f"{scheme}://private-user:private-password@mongo.example/"
        "?replicaSet=rs0"
    )

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct MongoDB URI was opened as a store")

    monkeypatch.setattr("pollard.cli.MongoStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct MongoDB URI store specs are not supported" in captured.err
    for secret in (spec, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("missing", "shown"),
    [
        ("POLLARD_NEO4J_URI", "POLLARD_NEO4J_URI"),
        ("POLLARD_NEO4J_USER", "POLLARD_NEO4J_USER"),
        ("POLLARD_NEO4J_PASSWORD", "POLLARD_NEO4J_PASSWORD"),
        ("MISSING\u2028FORGED", "MISSING%E2%80%A8FORGED"),
    ],
)
def test_cli_neo4j_env_requires_configured_variables_without_leaking_values(
    missing: str,
    shown: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri_variable = (
        missing if missing == "MISSING\u2028FORGED" else "POLLARD_NEO4J_URI"
    )
    spec = (
        f"neo4j-env:{uri_variable}"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD#team"
    )
    values = {
        "POLLARD_NEO4J_URI": "neo4j+s://private-graph.example",
        "POLLARD_NEO4J_USER": "private-user",
        "POLLARD_NEO4J_PASSWORD": "private-password",
    }
    for variable, value in values.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.delenv(missing, raising=False)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    transcript = captured.out + captured.err
    assert captured.out == ""
    assert shown in captured.err
    assert "\u2028" not in transcript
    assert len(captured.err.splitlines()) == 1
    for secret in values.values():
        assert secret not in transcript


def test_cli_neo4j_env_defaults_database_and_store_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "neo4j+s://private-graph.example"
    username = "private-user"
    password = "private-password"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
    )
    backing = MemoryStore()
    run = Runtime(backing).run("neo4j-defaults")
    constructed: list[tuple[str, tuple[str, str], str, str]] = []

    class FakeNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_uri, auth, database, store_id))

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == run.root_id
    assert (
        listed["runs"][0]["store"]
        == "neo4j-env:POLLARD_NEO4J_URI?database=neo4j#default"
    )
    assert constructed == [
        (uri, (username, password), "neo4j", "default")
    ]
    transcript = captured.out + captured.err
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in transcript


def test_cli_observation_commands_accept_neo4j_env_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "bolt+s://private-graph.example"
    username = "private-user"
    password = "private-password"
    database = "tenant"
    store_id = "team"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        f"&database={database}#{store_id}"
    )
    backing = MemoryStore()
    run = Runtime(backing).run("neo4j-cli")
    run.model_call(
        {"model": "mock-1", "prompt": "inspect this run"},
        fn=lambda _payload: {
            "text": "stored result",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    root_id = run.root_id
    constructed: list[tuple[str, tuple[str, str], str, str]] = []
    entered: list[tuple[str, str]] = []
    exited: list[tuple[str, str]] = []

    class FakeNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_uri, auth, database, store_id))
            self.database = database
            self.store_id = store_id

        def __enter__(self) -> MemoryStore:
            entered.append((self.database, self.store_id))
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append((self.database, self.store_id))

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    shown = json.loads(captured.out)
    assert shown["root_id"] == root_id
    assert len(shown["nodes"]) == 2

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    reported = json.loads(captured.out)
    assert reported["root_id"] == root_id
    assert reported["nodes"] == 2
    assert reported["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]
    assert verified["nodes"] == 2

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)
    assert sealed["root_id"] == root_id
    assert len(sealed["digest"]) == 64

    export_path = tmp_path / "neo4j-subtree.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    manifest_text = export_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert exported == {
        "digest": sealed["digest"],
        "nodes": 2,
        "path": str(export_path),
        "root_id": root_id,
    }
    assert manifest["root_id"] == root_id
    assert manifest["seal"]["digest"] == sealed["digest"]

    expected = (uri, (username, password), database, store_id)
    assert constructed == [expected] * 5
    assert entered == [(database, store_id)] * 5
    assert exited == [(database, store_id)] * 5
    exposed = "".join(transcript) + manifest_text
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in exposed


def test_cli_runs_and_merge_accept_neo4j_env_as_source_with_canonical_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+ssc://private-graph.example"
    username = "private-user"
    password = "private-password"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant"
        "&user-env=POLLARD_NEO4J_USER#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    backing = MemoryStore()
    run = Runtime(backing).run("neo4j-source")
    run.note({"label": "merge-me"})
    root_id = run.root_id
    constructed: list[tuple[str, tuple[str, str], str, str]] = []

    class FakeNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            constructed.append((configured_uri, auth, database, store_id))

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)
    transcript: list[str] = []

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == root_id
    assert listed["runs"][0]["store"] == label

    destination = tmp_path / "neo4j-merged.db"
    assert main(["merge", str(destination), spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    merged = json.loads(captured.out)
    assert merged["copied"] == 2
    assert merged["sources"][0]["source"] == label
    with SQLiteStore(destination) as store:
        assert store.exists(root_id)
        assert len(list(store.walk(root_id))) == 2

    assert constructed == [
        (uri, (username, password), "tenant", "team")
    ] * 2
    exposed = "".join(transcript)
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in exposed


def test_cli_merge_into_existing_neo4j_destination_records_conflicts_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    source_path = tmp_path / "neo4j-conflicting-source.db"
    backing = MemoryStore()
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "neo4j-existing"},
    )
    left = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "left"},
        meta={"worker": "left", "nested": {"a": 1}},
    )
    right = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "right"},
        meta={"worker": "right", "nested": {"b": 2}},
    )
    backing.put(root)
    backing.put(left)
    with SQLiteStore(source_path) as source:
        source.put(root)
        source.put(right)

    class FakeNeo4jStore:
        def __init__(self, _uri: str, _auth: object, **options: object) -> None:
            assert options["create"] is False

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("POLLARD_NEO4J_URI", "neo4j+s://private.example")
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["existing"] == 2
    assert first["result_conflicts"] == 1
    assert first["meta_conflicts"] == 1
    snapshot = list(backing.walk(root.id))

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert list(backing.walk(root.id)) == snapshot


def test_cli_neo4j_source_failure_prevents_neo4j_destination_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = (
        "neo4j-env:POLLARD_SOURCE_NEO4J"
        "?user-env=POLLARD_SOURCE_NEO4J_USER"
        "&password-env=POLLARD_SOURCE_NEO4J_PASSWORD"
        "&database=source_db#source-store"
    )
    destination = (
        "neo4j-env:POLLARD_DESTINATION_NEO4J"
        "?user-env=POLLARD_DESTINATION_NEO4J_USER"
        "&password-env=POLLARD_DESTINATION_NEO4J_PASSWORD"
        "&database=destination_db#destination-store"
    )
    source_uri = "neo4j+s://source.example"
    destination_uri = "neo4j+s://destination.example"
    neo4j_failure = type(
        "ServiceUnavailable",
        (Exception,),
        {"__module__": "neo4j.exceptions"},
    )
    constructed: list[tuple[str, bool]] = []

    class FailingSourceNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            _auth: object,
            **options: object,
        ) -> None:
            constructed.append((configured_uri, bool(options["create"])))
            assert configured_uri == source_uri

        def __enter__(self) -> "FailingSourceNeo4jStore":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def roots(self) -> list[str]:
            raise neo4j_failure(f"source failed for {source_uri}")

    values = {
        "POLLARD_SOURCE_NEO4J": source_uri,
        "POLLARD_SOURCE_NEO4J_USER": "source-user",
        "POLLARD_SOURCE_NEO4J_PASSWORD": "source-password",
        "POLLARD_DESTINATION_NEO4J": destination_uri,
        "POLLARD_DESTINATION_NEO4J_USER": "destination-user",
        "POLLARD_DESTINATION_NEO4J_PASSWORD": "destination-password",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "pollard.cli.Neo4jStore",
        FailingSourceNeo4jStore,
    )

    assert (
        main(
            [
                "merge",
                destination,
                source,
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not access neo4j-env:POLLARD_SOURCE_NEO4J" in captured.err
    assert "POLLARD_DESTINATION_NEO4J" not in captured.err
    assert constructed == [(source_uri, False)]
    for secret in values.values():
        assert secret not in captured.err


def test_cli_neo4j_source_walk_failure_prevents_destination_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_uri = "neo4j+s://source-private.example"
    destination_uri = "neo4j+s://destination-private.example"
    source = (
        "neo4j-env:POLLARD_SOURCE_NEO4J"
        "?user-env=POLLARD_SOURCE_NEO4J_USER"
        "&password-env=POLLARD_SOURCE_NEO4J_PASSWORD"
        "&database=tenant#source"
    )
    destination = (
        "neo4j-env:POLLARD_DESTINATION_NEO4J"
        "?user-env=POLLARD_DESTINATION_NEO4J_USER"
        "&password-env=POLLARD_DESTINATION_NEO4J_PASSWORD"
        "&database=tenant#destination"
    )
    neo4j_failure = type(
        "SessionExpired",
        (Exception,),
        {"__module__": "neo4j.exceptions"},
    )
    constructed: list[tuple[str, bool]] = []

    class FailingWalkNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            _auth: object,
            **options: object,
        ) -> None:
            constructed.append((configured_uri, bool(options["create"])))
            if configured_uri == destination_uri:
                raise AssertionError("Neo4j destination was opened")

        def __enter__(self) -> "FailingWalkNeo4jStore":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def roots(self) -> list[str]:
            return ["root"]

        def walk(self, _root_id: str) -> Iterator[Node]:
            raise neo4j_failure(
                f"source traversal failed for {source_uri}"
            )
            yield

    environment = {
        "POLLARD_SOURCE_NEO4J": source_uri,
        "POLLARD_SOURCE_NEO4J_USER": "source-user",
        "POLLARD_SOURCE_NEO4J_PASSWORD": "source-password",
        "POLLARD_DESTINATION_NEO4J": destination_uri,
        "POLLARD_DESTINATION_NEO4J_USER": "destination-user",
        "POLLARD_DESTINATION_NEO4J_PASSWORD": "destination-password",
    }
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FailingWalkNeo4jStore)

    assert (
        main(
            [
                "merge",
                destination,
                source,
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "could not access "
        "neo4j-env:POLLARD_SOURCE_NEO4J?database=tenant#source"
        in captured.err
    )
    assert constructed == [(source_uri, False)]
    for secret in environment.values():
        assert secret not in captured.err


def test_cli_neo4j_destination_constructor_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+s://private-graph.example"
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    source = tmp_path / "neo4j-constructor-source.db"
    _recording(source)

    class FailingNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            **options: object,
        ) -> None:
            assert options["create"] is True
            raise ValueError(
                f"failed {configured_uri} as {auth[0]}:{auth[1]}"
            )

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", FailingNeo4jStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_neo4j_destination_constructor_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+s://private-graph.example"
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    source = tmp_path / "neo4j-constructor-warning-source.db"
    _recording(source)
    closed: list[bool] = []

    class WarningNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            **options: object,
        ) -> None:
            assert options["create"] is True
            warnings.warn(
                f"warning for {configured_uri} as {auth[0]}:{auth[1]}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", WarningNeo4jStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert closed == [True]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_neo4j_destination_body_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+s://private-graph.example"
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    source = tmp_path / "neo4j-body-warning-source.db"
    _recording(source)
    exited: list[type[BaseException] | None] = []

    class WarningDestination:
        def exists(self, _node_id: str) -> bool:
            warnings.warn(
                f"warning for {uri} as private-user:private-password",
                UserWarning,
                stacklevel=2,
            )
            return False

    class FakeNeo4jStore:
        def __init__(self, _uri: str, _auth: object, **_options: object) -> None:
            return None

        def __enter__(self) -> WarningDestination:
            return WarningDestination()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type)

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert exited == [UserWarning]
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


def test_cli_neo4j_destination_driver_write_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+s://private-graph.example"
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    source = tmp_path / "neo4j-write-source.db"
    _recording(source)
    neo4j_failure = type(
        "ServiceUnavailable",
        (Exception,),
        {"__module__": "neo4j.exceptions"},
    )

    class FailingDestination:
        def exists(self, _node_id: str) -> bool:
            return False

        def put(self, _node: Node) -> None:
            raise neo4j_failure(
                f"write failed for {uri} as private-user:private-password"
            )

    class FakeNeo4jStore:
        def __init__(self, _uri: str, _auth: object, **_options: object) -> None:
            return None

        def __enter__(self) -> FailingDestination:
            return FailingDestination()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("suffix", "message"),
    [
        (
            "?password-env=POLLARD_NEO4J_PASSWORD#team",
            "user-env",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER#team",
            "password-env",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD&unknown=value#team",
            "accepts only",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER"
            "&user-env=OTHER_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD#team",
            "once",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&password-env=OTHER_PASSWORD#team",
            "once",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=one&database=two#team",
            "once",
        ),
        (
            "?user-env=&password-env=POLLARD_NEO4J_PASSWORD#team",
            "must name an environment variable",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER&password-env=#team",
            "must name an environment variable",
        ),
        (
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD&database=#team",
            "non-empty",
        ),
        (
            "?user-env&password-env=POLLARD_NEO4J_PASSWORD#team",
            "invalid query",
        ),
        (
            "#team?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD",
            "query must precede",
        ),
    ],
)
def test_cli_rejects_invalid_neo4j_env_queries(
    suffix: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_neo4j(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Neo4j spec was opened")

    monkeypatch.setenv("POLLARD_NEO4J_URI", "neo4j://graph.example")
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", unexpected_neo4j)

    assert (
        main(
            [
                "runs",
                f"neo4j-env:POLLARD_NEO4J_URI{suffix}",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    for secret in (
        "neo4j://graph.example",
        "private-user",
        "private-password",
    ):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("spec", "control"),
    [
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant%0AFORGED#team",
            "\nFORGED",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER%0AFORGED"
            "&password-env=POLLARD_NEO4J_PASSWORD#team",
            "\nFORGED",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD#team\x1b[31mFORGED",
            "\x1b",
        ),
    ],
    ids=["database", "auth-reference", "store-id"],
)
def test_cli_rejects_control_characters_in_neo4j_namespaces(
    spec: str,
    control: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "neo4j+s://private-graph.example"

    def unexpected_neo4j(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("hostile Neo4j spec was opened")

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", unexpected_neo4j)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    transcript = captured.out + captured.err
    assert captured.out == ""
    assert control not in transcript
    assert "control characters" in captured.err
    for secret in (uri, "private-user", "private-password"):
        assert secret not in transcript


def test_cli_neo4j_constructor_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "neo4j+s://private-graph.example"
    username = "private-user"
    password = "private-password"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"

    class FailingNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert auth == (username, password)
            assert (database, store_id) == ("tenant", "team")
            raise ValueError(
                f"could not connect to {configured_uri} as {auth[0]}:{auth[1]}"
            )

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FailingNeo4jStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in captured.err


def test_cli_neo4j_body_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "bolt+s://private-graph.example"
    username = "private-user"
    password = "private-password"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    neo4j_failure = type(
        "ServiceUnavailable",
        (Exception,),
        {"__module__": "neo4j.exceptions"},
    )
    exited: list[bool] = []

    class FailingStore:
        def get(self, _node_id: str) -> Node:
            raise neo4j_failure(
                f"connection failed for {uri} as {username}:{password}"
            )

    class FakeNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert auth == (username, password)
            assert (database, store_id) == ("tenant", "team")

        def __enter__(self) -> FailingStore:
            return FailingStore()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type is neo4j_failure)

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert main(["show", spec, "missing-root", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert exited == [True]
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in captured.err


def test_cli_neo4j_constructor_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri = "neo4j+ssc://private-graph.example"
    username = "private-user"
    password = "private-password"
    spec = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=neo4j#team"
    closed: list[bool] = []

    class WarningNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            assert create is False
            assert configured_uri == uri
            assert auth == (username, password)
            assert (database, store_id) == ("neo4j", "team")
            warnings.warn(
                f"Neo4j warning exposed {configured_uri} {auth[0]} {auth[1]}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

        def __enter__(self) -> object:
            raise AssertionError("warning-producing Neo4jStore was entered")

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", WarningNeo4jStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert closed == [True]
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("missing", "message", "reference"),
    [
        (
            "UNSET_NEO4J_URI",
            "Neo4j URI environment variable is not set",
            "UNSET_NEO4J_URI",
        ),
        (
            "UNSET_NEO4J_USER",
            "Neo4j user environment variable is not set",
            "UNSET_NEO4J_USER",
        ),
        (
            "UNSET_NEO4J_PASSWORD",
            "Neo4j password environment variable is not set",
            "UNSET_NEO4J_PASSWORD",
        ),
    ],
)
def test_cli_neo4j_destination_missing_environment_after_source_preflight(
    missing: str,
    message: str,
    reference: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "neo4j-env:UNSET_NEO4J_URI"
        "?user-env=UNSET_NEO4J_USER"
        "&password-env=UNSET_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    source = tmp_path / "source.db"
    _recording(source)

    def unexpected_neo4j(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Neo4j destination was opened")

    environment = {
        "UNSET_NEO4J_URI": "neo4j+s://private-graph.example",
        "UNSET_NEO4J_USER": "private-user",
        "UNSET_NEO4J_PASSWORD": "private-password",
    }
    for variable, value in environment.items():
        if variable == missing:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
    monkeypatch.setattr("pollard.cli.Neo4jStore", unexpected_neo4j)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert reference in captured.err
    for secret in environment.values():
        assert secret not in captured.err


def test_cli_merge_accepts_neo4j_env_destination_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = "neo4j+s://private-graph.example"
    username = "private-user"
    password = "private-password"
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    label = "neo4j-env:POLLARD_NEO4J_URI?database=tenant#team"
    source = tmp_path / "neo4j-destination-source.db"
    root_id, _payload = _recording(source)
    backing = MemoryStore()
    constructed: list[
        tuple[str, tuple[str, str], str, str, bool]
    ] = []

    class FakeNeo4jStore:
        def __init__(
            self,
            configured_uri: str,
            auth: tuple[str, str],
            *,
            database: str = "neo4j",
            store_id: str = "default",
            create: bool = True,
        ) -> None:
            constructed.append(
                (configured_uri, auth, database, store_id, create)
            )

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("POLLARD_NEO4J_URI", uri)
    monkeypatch.setenv("POLLARD_NEO4J_USER", username)
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", password)
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)
    transcript: list[str] = []

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    first = json.loads(captured.out)
    assert first["destination"] == label
    assert first["copied"] == 2
    assert first["existing"] == 0

    assert main(["merge", destination, str(source), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    repeated = json.loads(captured.out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert len(list(backing.walk(root_id))) == 2
    assert constructed == [
        (uri, (username, password), "tenant", "team", True),
        (uri, (username, password), "tenant", "team", False),
    ]
    exposed = "".join(transcript)
    for secret in (
        uri,
        username,
        password,
        "POLLARD_NEO4J_USER",
        "POLLARD_NEO4J_PASSWORD",
    ):
        assert secret not in exposed


@pytest.mark.parametrize(
    "scheme",
    ["neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc", "NEO4J"],
)
def test_cli_rejects_direct_neo4j_uri_as_merge_destination_without_connecting(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = f"{scheme}://private-user:private-password@graph.example"
    source = tmp_path / "neo4j-direct-source.db"
    _recording(source)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct Neo4j destination was opened")

    monkeypatch.setattr("pollard.cli.Neo4jStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct Neo4j or Bolt URI" in captured.err
    assert "neo4j-env:" in captured.err
    for secret in (destination, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "destination",
    [
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD#team"
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant"
        ),
    ],
    ids=["missing-database", "missing-store-id"],
)
def test_cli_neo4j_destination_requires_explicit_namespace_before_access(
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"
    monkeypatch.delenv("POLLARD_NEO4J_URI", raising=False)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "require explicit" in captured.err
    assert "database, and store id" in captured.err
    assert not source.exists()


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        (
            "neo4j-env:"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant#team",
            "requires a URI environment variable",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant#team",
            "requires user-env and password-env",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&database=tenant#team",
            "requires user-env and password-env",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env="
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant#team",
            "must name an environment variable",
        ),
    ],
    ids=["missing-uri-reference", "missing-user", "missing-password", "empty-user"],
)
def test_cli_neo4j_destination_requires_explicit_connection_references(
    destination: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not source.exists()


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant#team%ZZ",
            "invalid neo4j-env store spec",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant%20db#team",
            "must not contain whitespace",
        ),
        (
            "neo4j-env:POLLARD_NEO4J_URI"
            "?user-env=POLLARD_NEO4J_USER"
            "&password-env=POLLARD_NEO4J_PASSWORD"
            "&database=tenant#team%20one",
            "must not contain whitespace",
        ),
    ],
)
def test_cli_neo4j_destination_rejects_ambiguous_selector_before_access(
    destination: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"
    monkeypatch.delenv("POLLARD_NEO4J_URI", raising=False)

    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not source.exists()


def test_cli_preflights_sources_before_creating_neo4j_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "neo4j-env:POLLARD_NEO4J_URI"
        "?user-env=POLLARD_NEO4J_USER"
        "&password-env=POLLARD_NEO4J_PASSWORD"
        "&database=tenant#team"
    )
    source = tmp_path / "neo4j-preflight-source.db"
    missing = tmp_path / "neo4j-missing-source.db"
    _recording(source)
    constructed: list[bool] = []

    class FakeNeo4jStore:
        def __init__(
            self,
            _uri: str,
            _auth: object,
            **options: object,
        ) -> None:
            constructed.append(bool(options["create"]))

    monkeypatch.setenv("POLLARD_NEO4J_URI", "neo4j+s://private.example")
    monkeypatch.setenv("POLLARD_NEO4J_USER", "private-user")
    monkeypatch.setenv("POLLARD_NEO4J_PASSWORD", "private-password")
    monkeypatch.setattr("pollard.cli.Neo4jStore", FakeNeo4jStore)

    assert (
        main(
            [
                "merge",
                destination,
                str(source),
                str(missing),
                "--initialize-if-missing",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "neo4j-missing-source.db" in captured.err
    assert constructed == []


@pytest.mark.parametrize(
    "scheme",
    [
        "neo4j",
        "neo4j+s",
        "neo4j+ssc",
        "bolt",
        "bolt+s",
        "bolt+ssc",
    ],
)
def test_cli_rejects_direct_neo4j_uri_without_sqlite_fallback(
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = f"{scheme}://private-user:private-password@graph.example"

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct Neo4j URI was opened as a store")

    monkeypatch.setattr("pollard.cli.Neo4jStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct Neo4j URI store specs are not supported" in captured.err
    for secret in (spec, "private-user", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "spec",
    [
        " neo4j://private-user:SENTINEL@graph.example",
        "\tneo4j://private-user:SENTINEL@graph.example",
        "\nneo4j://private-user:SENTINEL@graph.example",
        "neo4j:\n//private-user:SENTINEL@graph.example",
        "neo4j:\t//private-user:SENTINEL@graph.example",
        "neo4j:/\n/private-user:SENTINEL@graph.example",
        "neo4j+ssc:\r//private-user:SENTINEL@graph.example",
        "bolt+s:\t//private-user:SENTINEL@graph.example",
    ],
    ids=[
        "leading-space",
        "leading-tab",
        "leading-newline",
        "scheme-newline",
        "scheme-tab",
        "slash-newline",
        "secure-scheme-carriage-return",
        "bolt-scheme-tab",
    ],
)
@pytest.mark.parametrize(
    "operation",
    ["source", "destination", "import", "gc"],
)
def test_cli_rejects_obfuscated_neo4j_uri_before_output_or_filesystem_access(
    spec: str,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.db"
    root_id, _payload = _recording(source)
    manifest = tmp_path / "subtree.json"
    if operation == "import":
        assert main(["export", str(source), root_id, str(manifest), "--json"]) == 0
        capsys.readouterr()

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    if operation == "source":
        arguments = ["runs", spec, "--json"]
    elif operation == "destination":
        arguments = ["merge", spec, str(source), "--json"]
    elif operation == "import":
        arguments = ["import", str(manifest), spec, "--json"]
    else:
        arguments = ["gc", spec, "compact", "--json"]

    assert main(arguments) == 2
    captured = capsys.readouterr()
    transcript = captured.out + captured.err
    assert captured.out == ""
    assert "schemes must not contain whitespace or control characters" in captured.err
    assert len(captured.err.splitlines()) == 1
    for secret in ("private-user", "SENTINEL", "graph.example"):
        assert secret not in transcript
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before


def test_cli_kafka_env_defaults_and_sanitizes_client_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broker = "private-broker.example:9093"
    username = "private-user"
    password = "private-password"
    spec = "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard.events"
    configured = {
        "bootstrap.servers": broker,
        "security.protocol": "SASL_SSL",
        "sasl.username": username,
        "sasl.password": password,
        "debug": "private-debug",
        "aws_debug": "private-aws-debug",
        "log_level": 7,
        "request.timeout.ms": 1200,
        "enable.auto.commit": False,
        "compression.ratio": 1.25,
    }
    expected_config = {
        "bootstrap.servers": broker,
        "security.protocol": "SASL_SSL",
        "sasl.username": username,
        "sasl.password": password,
        "log_level": 0,
        "request.timeout.ms": 1200,
        "enable.auto.commit": False,
        "compression.ratio": 1.25,
    }
    backing = MemoryStore()
    run = Runtime(backing).run("kafka-defaults")
    constructed: list[tuple[dict[str, object], str, str, bool, int]] = []

    class FakeKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            store_id: str = "default",
            read_only: bool = False,
            timeout: int = 30,
        ) -> None:
            constructed.append(
                (
                    dict(client_config),
                    topic,
                    store_id,
                    read_only,
                    timeout,
                )
            )

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv("POLLARD_KAFKA_CONFIG", json.dumps(configured))
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == run.root_id
    assert (
        listed["runs"][0]["store"] == "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard.events#default"
    )
    assert constructed == [
        (
            expected_config,
            "pollard.events",
            "default",
            True,
            30,
        )
    ]
    transcript = captured.out + captured.err
    for secret in (
        broker,
        username,
        password,
        "private-debug",
        "private-aws-debug",
    ):
        assert secret not in transcript


def test_cli_source_commands_and_merge_accept_reordered_kafka_env_spec(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    configured = {
        "bootstrap.servers": broker,
        "security.protocol": "SASL_SSL",
        "sasl.password": password,
    }
    expected_config = {**configured, "log_level": 0}
    spec = "kafka-env:POLLARD_KAFKA_CONFIG?timeout=17&topic=tenant.audit#team%2Fone"
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team%2Fone"
    backing = MemoryStore()
    run = Runtime(backing).run("kafka-cli")
    run.model_call(
        {"model": "mock-1", "prompt": "inspect this run"},
        fn=lambda _payload: {
            "text": "stored result",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    root_id = run.root_id
    constructed: list[tuple[dict[str, object], str, str, bool, int]] = []
    entered: list[tuple[str, str]] = []
    exited: list[tuple[str, str]] = []

    class FakeKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            store_id: str = "default",
            read_only: bool = False,
            timeout: int = 30,
        ) -> None:
            constructed.append(
                (
                    dict(client_config),
                    topic,
                    store_id,
                    read_only,
                    timeout,
                )
            )
            self.topic = topic
            self.store_id = store_id

        def __enter__(self) -> MemoryStore:
            entered.append((self.topic, self.store_id))
            return backing

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            exited.append((self.topic, self.store_id))

    monkeypatch.setenv("POLLARD_KAFKA_CONFIG", json.dumps(configured))
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    shown = json.loads(captured.out)
    assert shown["root_id"] == root_id
    assert len(shown["nodes"]) == 2

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    reported = json.loads(captured.out)
    assert reported["root_id"] == root_id
    assert reported["nodes"] == 2
    assert reported["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]
    assert verified["nodes"] == 2

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)
    assert sealed["root_id"] == root_id
    assert len(sealed["digest"]) == 64

    export_path = tmp_path / "kafka-subtree.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    manifest_text = export_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert exported == {
        "digest": sealed["digest"],
        "nodes": 2,
        "path": str(export_path),
        "root_id": root_id,
    }
    assert manifest["root_id"] == root_id
    assert manifest["seal"]["digest"] == sealed["digest"]

    assert main(["runs", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    listed = json.loads(captured.out)
    assert listed["runs"][0]["root_id"] == root_id
    assert listed["runs"][0]["store"] == label

    destination = tmp_path / "kafka-merged.db"
    assert main(["merge", str(destination), spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    merged = json.loads(captured.out)
    assert merged["copied"] == 2
    assert merged["sources"][0]["source"] == label
    with SQLiteStore(destination) as store:
        assert store.exists(root_id)
        assert len(list(store.walk(root_id))) == 2

    expected = (
        expected_config,
        "tenant.audit",
        "team/one",
        True,
        17,
    )
    assert constructed == [expected] * 7
    assert entered == [("tenant.audit", "team/one")] * 7
    assert exited == [("tenant.audit", "team/one")] * 7
    exposed = "".join(transcript) + manifest_text
    for secret in (broker, password):
        assert secret not in exposed


@pytest.mark.parametrize(
    ("suffix", "message"),
    [
        ("", "non-empty topic"),
        ("?topic=", "non-empty topic"),
        ("?timeout=17", "non-empty topic"),
        ("?topic=one&unknown=value", "accepts only"),
        ("?topic=one&topic=two", "once"),
        ("?topic=one&timeout=1&timeout=2", "once"),
        ("?topic=one&timeout=", "positive integer"),
        ("?topic=one&timeout=0", "positive integer"),
        ("?topic=one&timeout=-1", "positive integer"),
        ("?topic=one&timeout=1.5", "positive integer"),
        (f"?topic=one&timeout={'9' * 400}", "positive integer"),
        ("?topic", "invalid query"),
        ("#team?topic=one", "query must precede"),
        ("?topic=tenant%0Aforged", "whitespace or control characters"),
    ],
)
def test_cli_rejects_invalid_kafka_env_queries(
    suffix: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_kafka(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Kafka spec was opened")

    configured = {
        "bootstrap.servers": "private-broker.example:9093",
        "sasl.password": "private-password",
    }
    monkeypatch.setenv("POLLARD_KAFKA_CONFIG", json.dumps(configured))
    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_kafka)

    assert (
        main(
            [
                "runs",
                f"kafka-env:POLLARD_KAFKA_CONFIG{suffix}",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    for secret in ("private-broker.example:9093", "private-password"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("raw_config", "message"),
    [
        (
            '{"bootstrap.servers":"private-malformed"',
            "valid JSON",
        ),
        (
            '{"bootstrap.servers":"private-one","bootstrap.servers":"private-two"}',
            "valid JSON",
        ),
        (
            '"private-non-object"',
            "JSON object",
        ),
        (
            '{"bootstrap.servers":"private-broker","nested":{"secret":"private-nested"}}',
            "strings, numbers, or booleans",
        ),
        (
            '{"bootstrap.servers":"private-broker","brokers":["private-secondary"]}',
            "strings, numbers, or booleans",
        ),
        (
            '{"bootstrap.servers":"private-broker","sasl.password":null}',
            "strings, numbers, or booleans",
        ),
        (
            '{"bootstrap.servers":"private-broker","linger.ms":NaN}',
            "valid JSON",
        ),
        (
            '{"sasl.password":"private-password"}',
            "non-empty 'bootstrap.servers' string",
        ),
        (
            '{"bootstrap.servers":"","sasl.password":"private-password"}',
            "non-empty 'bootstrap.servers' string",
        ),
        (
            '{"bootstrap.servers":9092,"sasl.password":"private-password"}',
            "non-empty 'bootstrap.servers' string",
        ),
        (
            '{"bootstrap.servers":"private-broker","bad key":"private-password"}',
            "invalid property name",
        ),
        (
            '{"bootstrap.servers":"private-broker","plugin.library.paths":"private-plugin"}',
            "does not accept plugin.library.paths",
        ),
    ],
)
def test_cli_rejects_malformed_or_unsafe_kafka_client_config(
    raw_config: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_kafka(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Kafka config was opened")

    monkeypatch.setenv("POLLARD_KAFKA_CONFIG", raw_config)
    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_kafka)

    assert (
        main(
            [
                "runs",
                "kafka-env:POLLARD_KAFKA_CONFIG?topic=pollard.events",
                "--json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "private-" not in captured.err


def test_cli_kafka_constructor_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    spec = "kafka-env:POLLARD_KAFKA_CONFIG?timeout=11&topic=tenant.audit#team"
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    closed: list[bool] = []

    class WarningKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            store_id: str = "default",
            read_only: bool = False,
            timeout: int = 30,
        ) -> None:
            assert client_config == {
                "bootstrap.servers": broker,
                "sasl.password": password,
                "log_level": 0,
            }
            assert (topic, store_id, read_only, timeout) == (
                "tenant.audit",
                "team",
                True,
                11,
            )
            warnings.warn(
                f"Kafka warning exposed {broker} {password}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

        def __enter__(self) -> object:
            raise AssertionError("warning-producing KafkaStore was entered")

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", WarningKafkaStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert closed == [True]
    for secret in (broker, password):
        assert secret not in captured.err


def test_cli_kafka_cimpl_body_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    spec = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    kafka_failure = type(
        "KafkaException",
        (Exception,),
        {"__module__": "cimpl"},
    )
    exited: list[bool] = []

    class FailingStore:
        def roots(self) -> list[str]:
            raise kafka_failure(f"connection failed for {broker} with {password}")

    class FakeKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            store_id: str = "default",
            read_only: bool = False,
            timeout: int = 30,
        ) -> None:
            assert client_config == {
                "bootstrap.servers": broker,
                "sasl.password": password,
                "log_level": 0,
            }
            assert (topic, store_id, read_only, timeout) == (
                "tenant.audit",
                "team",
                True,
                30,
            )

        def __enter__(self) -> FailingStore:
            return FailingStore()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type is kafka_failure)

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert exited == [True]
    for secret in (broker, password):
        assert secret not in captured.err


def test_cli_merge_accepts_existing_kafka_destination_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG"
        "?timeout=17&topic=tenant.audit#team"
    )
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    source = tmp_path / "kafka-destination-source.db"
    root_id, _payload = _recording(source)
    backing = MemoryStore()
    backing.put(
        Node.make(
            kind=NodeKind.ROOT,
            parent=None,
            payload={"run": "existing-kafka-destination"},
        )
    )
    constructed: list[tuple[str, str, bool, bool, int]] = []

    class FakeKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            store_id: str = "default",
            read_only: bool = False,
            require_existing: bool = False,
            timeout: int = 30,
        ) -> None:
            assert client_config == {
                "bootstrap.servers": broker,
                "sasl.password": password,
                "log_level": 0,
            }
            constructed.append(
                (topic, store_id, read_only, require_existing, timeout)
            )

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)
    transcript: list[str] = []

    assert main(["merge", destination, str(source), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    first = json.loads(captured.out)
    assert first["destination"] == label
    assert first["copied"] == 2
    assert first["existing"] == 0

    assert main(["merge", destination, str(source), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    repeated = json.loads(captured.out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert len(list(backing.walk(root_id))) == 2
    assert constructed == [
        ("tenant.audit", "team", False, True, 17),
        ("tenant.audit", "team", False, True, 17),
    ]
    exposed = "".join(transcript)
    for secret in (broker, password):
        assert secret not in exposed


def test_cli_merge_into_existing_kafka_destination_records_conflicts_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    source_path = tmp_path / "kafka-conflicting-source.db"
    backing = MemoryStore()
    root = Node.make(
        kind=NodeKind.ROOT,
        parent=None,
        payload={"run": "kafka-existing"},
    )
    left = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "left"},
        meta={"worker": "left", "nested": {"a": 1}},
    )
    right = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock"},
        result={"text": "right"},
        meta={"worker": "right", "nested": {"b": 2}},
    )
    backing.put(root)
    backing.put(left)
    with SQLiteStore(source_path) as source:
        source.put(root)
        source.put(right)

    class FakeKafkaStore:
        def __init__(
            self,
            _client_config: dict[str, object],
            **options: object,
        ) -> None:
            assert options["read_only"] is False
            assert options["require_existing"] is True

        def __getattr__(self, name: str) -> object:
            return getattr(backing, name)

        def __enter__(self) -> MemoryStore:
            return backing

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps({"bootstrap.servers": "private.example:9093"}),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["existing"] == 2
    assert first["result_conflicts"] == 1
    assert first["meta_conflicts"] == 1
    snapshot = list(backing.walk(root.id))

    assert main(["merge", destination, str(source_path), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["copied"] == 0
    assert repeated["existing"] == 2
    assert repeated["result_conflicts"] == 0
    assert repeated["meta_conflicts"] == 0
    assert list(backing.walk(root.id)) == snapshot


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        (
            "kafka-env:?topic=tenant.audit#team",
            "requires a client config environment variable",
        ),
        (
            "kafka-env:POLLARD_KAFKA_CONFIG#team",
            "requires a non-empty topic",
        ),
        (
            "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit",
            "require an explicit client config",
        ),
    ],
    ids=["missing-config-reference", "missing-topic", "missing-store-id"],
)
def test_cli_kafka_destination_requires_explicit_selector_before_access(
    destination: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-source.db"

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("incomplete Kafka destination was opened")

    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not source.exists()


def test_cli_kafka_destination_missing_environment_after_source_preflight(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "kafka-env:UNSET_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    source = tmp_path / "kafka-environment-source.db"
    _recording(source)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Kafka destination was opened")

    monkeypatch.delenv("UNSET_KAFKA_CONFIG", raising=False)
    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Kafka client config environment variable is not set" in captured.err
    assert "UNSET_KAFKA_CONFIG" in captured.err


def test_cli_rejects_direct_kafka_uri_as_merge_destination_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    destination = (
        "kafka://private-user:private-password@private-broker.example/"
        "private-topic#private-store"
    )
    source = tmp_path / "kafka-direct-source.db"
    _recording(source)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct Kafka destination was opened")

    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct Kafka broker store specs" in captured.err
    assert "use kafka-env:" in captured.err
    for secret in (
        destination,
        "private-user",
        "private-password",
        "private-broker.example",
        "private-topic",
        "private-store",
    ):
        assert secret not in captured.err


@pytest.mark.parametrize("phase", ["roots", "walk"])
def test_cli_kafka_source_failure_prevents_destination_access(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = (
        "kafka-env:POLLARD_SOURCE_KAFKA_CONFIG"
        "?topic=source.audit#source-store"
    )
    destination = (
        "kafka-env:POLLARD_DESTINATION_KAFKA_CONFIG"
        "?topic=destination.audit#destination-store"
    )
    source_broker = "source-private.example:9093"
    destination_broker = "destination-private.example:9093"
    kafka_failure = type(
        "KafkaException",
        (Exception,),
        {"__module__": "cimpl"},
    )
    constructed: list[tuple[str, bool]] = []

    class FailingSourceKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            topic: str,
            read_only: bool = False,
            **_options: object,
        ) -> None:
            constructed.append((topic, read_only))
            assert client_config["bootstrap.servers"] == source_broker

        def __enter__(self) -> "FailingSourceKafkaStore":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def roots(self) -> list[str]:
            if phase == "roots":
                raise kafka_failure(
                    f"source replay failed for {source_broker}"
                )
            return ["root"]

        def walk(self, _root_id: str) -> Iterator[Node]:
            raise kafka_failure(
                f"source traversal failed for {source_broker}"
            )
            yield

    monkeypatch.setenv(
        "POLLARD_SOURCE_KAFKA_CONFIG",
        json.dumps({"bootstrap.servers": source_broker}),
    )
    monkeypatch.setenv(
        "POLLARD_DESTINATION_KAFKA_CONFIG",
        json.dumps({"bootstrap.servers": destination_broker}),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FailingSourceKafkaStore)

    assert main(["merge", destination, source, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "could not access "
        "kafka-env:POLLARD_SOURCE_KAFKA_CONFIG"
        "?topic=source.audit#source-store"
        in captured.err
    )
    assert constructed == [("source.audit", True)]
    for secret in (source_broker, destination_broker):
        assert secret not in captured.err


def test_cli_kafka_destination_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    source = tmp_path / "kafka-warning-source.db"
    _recording(source)
    closed: list[bool] = []

    class WarningKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            read_only: bool,
            require_existing: bool,
            **_options: object,
        ) -> None:
            assert read_only is False
            assert require_existing is True
            warnings.warn(
                f"Kafka warning exposed {client_config}",
                UserWarning,
                stacklevel=2,
            )

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", WarningKafkaStore)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert closed == [True]
    for secret in (broker, password):
        assert secret not in captured.err


def test_cli_kafka_destination_constructor_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    source = tmp_path / "kafka-constructor-source.db"
    _recording(source)

    class FailingKafkaStore:
        def __init__(
            self,
            client_config: dict[str, object],
            *,
            read_only: bool,
            require_existing: bool,
            **_options: object,
        ) -> None:
            assert read_only is False
            assert require_existing is True
            raise ValueError(f"failed Kafka client {client_config}")

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FailingKafkaStore)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    for secret in (broker, password):
        assert secret not in captured.err


def test_cli_kafka_destination_body_warning_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    source = tmp_path / "kafka-body-warning-source.db"
    _recording(source)
    exited: list[type[BaseException] | None] = []

    class WarningDestination:
        def exists(self, _node_id: str) -> bool:
            warnings.warn(
                f"Kafka warning exposed {broker} {password}",
                UserWarning,
                stacklevel=2,
            )
            return False

    class FakeKafkaStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> WarningDestination:
            return WarningDestination()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object,
        ) -> None:
            exited.append(exc_type)

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    assert exited == [UserWarning]
    for secret in (broker, password):
        assert secret not in captured.err


def test_cli_kafka_destination_driver_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = "private-broker.example:9093"
    password = "private-password"
    destination = (
        "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    )
    label = "kafka-env:POLLARD_KAFKA_CONFIG?topic=tenant.audit#team"
    source = tmp_path / "kafka-driver-source.db"
    _recording(source)
    kafka_failure = type(
        "KafkaException",
        (Exception,),
        {"__module__": "confluent_kafka"},
    )

    class FailingDestination:
        def exists(self, _node_id: str) -> bool:
            raise kafka_failure(
                f"write failed for {broker} with {password}"
            )

    class FakeKafkaStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FailingDestination:
            return FailingDestination()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv(
        "POLLARD_KAFKA_CONFIG",
        json.dumps(
            {
                "bootstrap.servers": broker,
                "sasl.password": password,
            }
        ),
    )
    monkeypatch.setattr("pollard.cli.KafkaStore", FakeKafkaStore)
    assert main(["merge", destination, str(source), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"could not access {label}" in captured.err
    for secret in (broker, password):
        assert secret not in captured.err


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("import", "import accepts only a SQLite path"),
        ("gc", "gc accepts only a SQLite path"),
    ],
)
def test_cli_rejects_kafka_write_targets_before_environment_lookup(
    operation: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    spec = "kafka-env:UNSET_KAFKA_CONFIG?topic=private-topic#private-store"

    environment_get = os.environ.get

    def guarded_environment_lookup(
        key: str,
        default: object = None,
    ) -> object:
        if key == "UNSET_KAFKA_CONFIG":
            raise AssertionError("Kafka environment was read")
        return environment_get(key, default)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Kafka write target was opened")

    monkeypatch.delenv("UNSET_KAFKA_CONFIG", raising=False)
    monkeypatch.setattr(os.environ, "get", guarded_environment_lookup)
    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    if operation == "import":
        arguments = [
            "import",
            str(tmp_path / "missing-manifest.json"),
            spec,
            "--json",
        ]
    else:
        arguments = ["gc", spec, "compact", "--json"]

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "environment variable is not set" not in captured.err
    for secret in ("private-topic", "private-store"):
        assert secret not in captured.err
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before


def test_cli_preflights_kafka_sources_before_creating_merge_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "existing-source.db"
    with SQLiteStore(source) as store:
        Runtime(store).run("existing-source")
    destination = tmp_path / "must-not-be-created.db"
    kafka_spec = (
        "kafka-env:UNSET_KAFKA_CONFIG?topic=pollard.events#default"
    )
    monkeypatch.delenv("UNSET_KAFKA_CONFIG", raising=False)

    assert (
        main(
            [
                "merge",
                str(destination),
                str(source),
                kafka_spec,
                "--json",
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Kafka client config environment variable is not set" in captured.err
    assert not destination.exists()


def test_cli_rejects_direct_kafka_uri_without_environment_or_sqlite_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = (
        "kafka://private-user:private-password@private-broker.example/private-topic#private-store"
    )

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct Kafka URI was opened as a store")

    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)

    assert main(["runs", spec, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct Kafka broker store specs are not supported" in captured.err
    for secret in (
        spec,
        "private-user",
        "private-password",
        "private-broker.example",
        "private-topic",
        "private-store",
    ):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "operation",
    ["source", "destination", "import", "gc"],
)
def test_cli_rejects_obfuscated_kafka_spec_before_environment_or_filesystem_access(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    spec = "kafka-\nenv:UNSET_KAFKA_CONFIG?topic=private-topic#private-store"

    environment_get = os.environ.get

    def guarded_environment_lookup(
        key: str,
        default: object = None,
    ) -> object:
        if key == "UNSET_KAFKA_CONFIG":
            raise AssertionError("Kafka environment was read")
        return environment_get(key, default)

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("obfuscated Kafka store was opened")

    monkeypatch.delenv("UNSET_KAFKA_CONFIG", raising=False)
    monkeypatch.setattr(os.environ, "get", guarded_environment_lookup)
    monkeypatch.setattr("pollard.cli.KafkaStore", unexpected_store)
    monkeypatch.setattr("pollard.cli.SQLiteStore", unexpected_store)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    if operation == "source":
        arguments = ["runs", spec, "--json"]
    elif operation == "destination":
        arguments = [
            "merge",
            spec,
            str(tmp_path / "missing-source.db"),
            "--json",
        ]
    elif operation == "import":
        arguments = [
            "import",
            str(tmp_path / "missing-manifest.json"),
            spec,
            "--json",
        ]
    else:
        arguments = ["gc", spec, "compact", "--json"]

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "schemes must not contain whitespace or control characters" in captured.err
    assert len(captured.err.splitlines()) == 1
    for secret in ("private-topic", "private-store"):
        assert secret not in captured.err
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before


@pytest.mark.parametrize("command", ["show", "report", "verify", "seal", "export"])
def test_cli_read_commands_do_not_create_missing_sqlite_database(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing.db"
    export_path = tmp_path / "missing-subtree.json"

    def unexpected_postgres(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SQLite path was routed to PostgresStore")

    monkeypatch.setattr("pollard.cli.PostgresStore", unexpected_postgres)
    arguments = [command, str(database)]
    if command != "verify":
        arguments.append("missing-root")
    if command == "export":
        arguments.append(str(export_path))
    arguments.append("--json")

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing.db" in captured.err
    assert not database.exists()
    assert not export_path.exists()


def test_cli_read_commands_accept_configured_postgres_and_export_to_sqlite(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    dsn = os.environ.get("POLLARD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PostgreSQL is not configured")
    unique = uuid.uuid4().hex
    store_id = f"cli-{unique}"
    with PostgresStore(dsn, store_id=store_id) as store:
        run = Runtime(store).run(f"cli-postgres-{unique}")
        run.model_call(
            {"model": "mock-1", "prompt": "remote CLI acceptance"},
            fn=lambda _payload: {
                "text": "remote stored result",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
        root_id = run.root_id

    spec = f"pg-env:POLLARD_TEST_POSTGRES_DSN#{store_id}"
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["root_id"] == root_id

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)

    export_path = tmp_path / "postgres-acceptance.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    assert exported["digest"] == sealed["digest"]
    assert exported["nodes"] == 2

    imported_db = tmp_path / "postgres-imported.db"
    assert main(["import", str(export_path), str(imported_db), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["imported"] == 2

    assert main(["verify", str(imported_db), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    imported_verification = json.loads(captured.out)
    assert imported_verification["ok"] is True
    assert imported_verification["roots"] == [root_id]
    assert dsn not in "".join(transcript)


def test_cli_read_commands_accept_configured_redis_and_export_to_sqlite(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = os.environ.get("POLLARD_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis is not configured")
    unique = uuid.uuid4().hex
    prefix = f"pollard-cli-{unique}"
    store_id = f"cli-{unique}"
    with RedisStore(url, store_id=store_id, prefix=prefix) as store:
        run = Runtime(store).run(f"cli-redis-{unique}")
        run.model_call(
            {"model": "mock-1", "prompt": "remote CLI acceptance"},
            fn=lambda _payload: {
                "text": "remote stored result",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
        root_id = run.root_id

    spec = (
        f"redis-env:POLLARD_TEST_REDIS_URL?prefix={prefix}"
        f"#{store_id}"
    )
    transcript: list[str] = []

    assert main(["show", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["root_id"] == root_id

    assert main(["report", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["spent"]["tokens"] == 3

    assert main(["verify", spec, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    verified = json.loads(captured.out)
    assert verified["ok"] is True
    assert verified["roots"] == [root_id]

    assert main(["seal", spec, root_id, "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    sealed = json.loads(captured.out)

    export_path = tmp_path / "redis-acceptance.json"
    assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    exported = json.loads(captured.out)
    assert exported["digest"] == sealed["digest"]
    assert exported["nodes"] == 2

    imported_db = tmp_path / "redis-imported.db"
    assert main(["import", str(export_path), str(imported_db), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    assert json.loads(captured.out)["imported"] == 2

    assert main(["verify", str(imported_db), "--json"]) == 0
    captured = capsys.readouterr()
    transcript.extend((captured.out, captured.err))
    imported_verification = json.loads(captured.out)
    assert imported_verification["ok"] is True
    assert imported_verification["roots"] == [root_id]
    assert url not in "".join(transcript)


def test_cli_merges_into_configured_redis_destination_without_preflight_writes(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    url = os.environ.get("POLLARD_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis is not configured")
    from redis import Redis

    unique = uuid.uuid4().hex
    prefix = f"pollard-cli-destination-{unique}"
    store_id = f"cli-destination-{unique}"
    digest = hashlib.sha256(store_id.encode("utf-8")).hexdigest()
    namespace_pattern = f"{{pollard-{digest}}}:{prefix}:*"
    spec = (
        f"redis-env:POLLARD_TEST_REDIS_URL?prefix={prefix}"
        f"#{store_id}"
    )
    source = tmp_path / "redis-destination-source.db"
    missing = tmp_path / "redis-destination-missing.db"
    root_id, _payload = _recording(source)
    client = Redis.from_url(url, decode_responses=True)
    transcript: list[str] = []

    try:
        assert list(client.scan_iter(match=namespace_pattern)) == []

        assert main(["merge", spec, str(source), "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "identity is missing" in captured.err
        assert list(client.scan_iter(match=namespace_pattern)) == []

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    str(missing),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "redis-destination-missing.db" in captured.err
        assert list(client.scan_iter(match=namespace_pattern)) == []

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        first = json.loads(captured.out)
        assert first["destination"] == spec
        assert first["copied"] == 2
        assert first["existing"] == 0
        assert first["result_conflicts"] == 0
        assert first["meta_conflicts"] == 0

        with RedisStore(
            url,
            store_id=store_id,
            prefix=prefix,
            create=False,
        ) as destination:
            revision_key = destination._revision_key
            schema_key = destination._bucket_key("schema")
            nodes_key = destination._bucket_key("nodes")
            assert set(client.scan_iter(match=namespace_pattern)) == {
                revision_key,
                schema_key,
                nodes_key,
            }
            assert client.hget(schema_key, "redis-store-id") == store_id
            assert client.hget(schema_key, "version") == "1"
            assert destination.exists(root_id)
            assert len(list(destination.walk(root_id))) == 2
            before_repeat = (
                client.get(revision_key),
                client.hgetall(nodes_key),
            )

        assert main(["merge", spec, str(source), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        repeated = json.loads(captured.out)
        assert repeated["copied"] == 0
        assert repeated["existing"] == 2
        assert repeated["result_conflicts"] == 0
        assert repeated["meta_conflicts"] == 0
        assert (
            client.get(revision_key),
            client.hgetall(nodes_key),
        ) == before_repeat

        exposed = "".join(transcript)
        assert url not in exposed
    finally:
        namespace_keys = list(client.scan_iter(match=namespace_pattern))
        if namespace_keys:
            client.delete(*namespace_keys)
        client.close()


def test_cli_merges_into_configured_mongodb_destination_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = os.environ.get("POLLARD_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("MongoDB is not configured")
    from pymongo import MongoClient

    unique = uuid.uuid4().hex
    database = f"pollard_cli_destination_{unique}"
    collection_prefix = f"pollard_cli_{unique}"
    store_id = f"cli-destination-{unique}"
    variable = "POLLARD_CLI_DESTINATION_TEST_MONGODB_URI"
    spec = (
        f"mongo-env:{variable}?database={database}"
        f"&prefix={collection_prefix}#{store_id}"
    )
    records_name = f"{collection_prefix}_records"
    coordinators_name = f"{collection_prefix}_coordinators"
    source = tmp_path / "mongodb-destination-source.db"
    missing = tmp_path / "mongodb-destination-missing.db"
    root_id, _payload = _recording(source)
    monkeypatch.setenv(variable, uri)
    client = MongoClient(uri)
    transcript: list[str] = []
    databases_before = set(client.list_database_names())
    if database in databases_before:
        client.close()
        pytest.skip("unique MongoDB acceptance database already exists")

    try:
        assert database not in databases_before

        assert main(["merge", spec, str(source), "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "schema version: missing" in captured.err
        assert database not in client.list_database_names()

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    str(missing),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "mongodb-destination-missing.db" in captured.err
        assert database not in client.list_database_names()

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        first = json.loads(captured.out)
        assert first["copied"] == 2
        assert first["existing"] == 0
        assert first["result_conflicts"] == 0
        assert first["meta_conflicts"] == 0

        selected = client[database]
        assert set(selected.list_collection_names()) == {
            records_name,
            coordinators_name,
        }
        indexes = selected[records_name].index_information()
        matching = [
            details
            for details in indexes.values()
            if list(details.get("key", []))
            == [
                ("store_id", 1),
                ("bucket", 1),
                ("key", 1),
            ]
        ]
        assert len(matching) == 1
        assert matching[0].get("unique") is True

        with MongoStore(
            uri,
            database=database,
            store_id=store_id,
            collection_prefix=collection_prefix,
            create=False,
        ) as destination:
            assert destination.exists(root_id)
            assert len(list(destination.walk(root_id))) == 2

        before_repeat = (
            list(selected[records_name].find().sort("_id", 1)),
            list(selected[coordinators_name].find().sort("_id", 1)),
            selected[records_name].index_information(),
        )
        assert main(["merge", spec, str(source), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        repeated = json.loads(captured.out)
        assert repeated["copied"] == 0
        assert repeated["existing"] == 2
        assert repeated["result_conflicts"] == 0
        assert repeated["meta_conflicts"] == 0
        assert (
            list(selected[records_name].find().sort("_id", 1)),
            list(selected[coordinators_name].find().sort("_id", 1)),
            selected[records_name].index_information(),
        ) == before_repeat

        assert uri not in "".join(transcript)
    finally:
        if database not in databases_before:
            client.drop_database(database)
        client.close()


def test_cli_accepts_configured_mongodb_for_observation_export_and_merge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = os.environ.get("POLLARD_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("MongoDB is not configured")
    from pymongo import MongoClient

    database = os.environ.get(
        "POLLARD_TEST_MONGODB_DATABASE",
        "pollard_test",
    )
    unique = uuid.uuid4().hex
    collection_prefix = f"pollard_cli_{unique}"
    store_id = f"cli-{unique}"
    variable = "POLLARD_CLI_TEST_MONGODB_URI"
    monkeypatch.setenv(variable, uri)
    client = MongoClient(uri)

    try:
        with MongoStore(
            uri,
            database=database,
            store_id=store_id,
            collection_prefix=collection_prefix,
        ) as store:
            run = Runtime(store).run(f"cli-mongodb-{unique}")
            run.model_call(
                {"model": "mock-1", "prompt": "remote CLI acceptance"},
                fn=lambda _payload: {
                    "text": "remote stored result",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            )
            root_id = run.root_id

        spec = (
            f"mongo-env:{variable}"
            f"?database={quote(database, safe='')}"
            f"&prefix={collection_prefix}#{quote(store_id, safe='')}"
        )
        transcript: list[str] = []

        assert main(["show", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["root_id"] == root_id

        assert main(["report", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["spent"]["tokens"] == 3

        assert main(["verify", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        verified = json.loads(captured.out)
        assert verified["ok"] is True
        assert verified["roots"] == [root_id]

        assert main(["seal", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        sealed = json.loads(captured.out)

        export_path = tmp_path / "mongodb-acceptance.json"
        assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        exported = json.loads(captured.out)
        assert exported["digest"] == sealed["digest"]
        assert exported["nodes"] == 2

        assert main(["runs", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        listed = json.loads(captured.out)
        assert listed["runs"][0]["root_id"] == root_id

        imported_db = tmp_path / "mongodb-imported.db"
        assert main(["import", str(export_path), str(imported_db), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["imported"] == 2

        assert main(["verify", str(imported_db), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        imported_verification = json.loads(captured.out)
        assert imported_verification["ok"] is True
        assert imported_verification["roots"] == [root_id]

        merged_db = tmp_path / "mongodb-merged.db"
        assert main(["merge", str(merged_db), spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        merged = json.loads(captured.out)
        assert merged["copied"] == 2
        with SQLiteStore(merged_db) as merged_store:
            assert merged_store.exists(root_id)

        assert uri not in "".join(transcript)
    finally:
        client[database].drop_collection(f"{collection_prefix}_records")
        client[database].drop_collection(
            f"{collection_prefix}_coordinators"
        )
        client.close()


def test_cli_merges_into_configured_neo4j_destination_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = os.environ.get("POLLARD_TEST_NEO4J_URI")
    password = os.environ.get("POLLARD_TEST_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("Neo4j is not configured")
    from neo4j import GraphDatabase

    username = os.environ.get("POLLARD_TEST_NEO4J_USER", "neo4j")
    database = os.environ.get("POLLARD_TEST_NEO4J_DATABASE", "neo4j")
    unique = uuid.uuid4().hex
    store_id = f"cli-destination-{unique}"
    uri_variable = "POLLARD_CLI_DESTINATION_TEST_NEO4J_URI"
    user_variable = "POLLARD_CLI_DESTINATION_TEST_NEO4J_USER"
    password_variable = "POLLARD_CLI_DESTINATION_TEST_NEO4J_PASSWORD"
    spec = (
        f"neo4j-env:{uri_variable}"
        f"?user-env={user_variable}"
        f"&password-env={password_variable}"
        f"&database={quote(database, safe='')}#{quote(store_id, safe='')}"
    )
    source = tmp_path / "neo4j-destination-source.db"
    missing = tmp_path / "neo4j-destination-missing.db"
    root_id, _payload = _recording(source)
    monkeypatch.setenv(uri_variable, uri)
    monkeypatch.setenv(user_variable, username)
    monkeypatch.setenv(password_variable, password)
    driver = GraphDatabase.driver(uri, auth=(username, password))
    transcript: list[str] = []

    def snapshot() -> tuple[dict[str, object], ...]:
        with driver.session(database=database) as session:
            return tuple(
                record.data()
                for record in session.run(
                    """
                    MATCH (node)
                    WHERE node.store_id = $store_id
                    RETURN labels(node) AS labels, properties(node) AS properties
                    ORDER BY coalesce(
                        node.record_key,
                        node.coordinator_key
                    )
                    """,
                    store_id=store_id,
                )
            )

    def constraint_snapshot() -> tuple[dict[str, object], ...]:
        with driver.session(database=database) as session:
            return tuple(
                record.data()
                for record in session.run(
                    """
                    SHOW CONSTRAINTS
                    YIELD name, type, entityType, labelsOrTypes,
                          properties, ownedIndex
                    WHERE name IN [
                        'pollard_neo4j_kv_record_key',
                        'pollard_neo4j_coordinator_key'
                    ]
                    RETURN name, type, entityType, labelsOrTypes,
                           properties, ownedIndex
                    ORDER BY name
                    """
                )
            )

    def index_snapshot() -> tuple[dict[str, object], ...]:
        with driver.session(database=database) as session:
            return tuple(
                record.data()
                for record in session.run(
                    """
                    SHOW INDEXES
                    YIELD name, state, type, entityType, labelsOrTypes,
                          properties, owningConstraint
                    WHERE name IN [
                        'pollard_neo4j_kv_record_key',
                        'pollard_neo4j_coordinator_key'
                    ]
                    RETURN name, state, type, entityType, labelsOrTypes,
                           properties, owningConstraint
                    ORDER BY name
                    """
                )
            )

    initial_constraints = constraint_snapshot()
    initial_indexes = index_snapshot()
    try:
        assert snapshot() == ()

        assert main(["merge", spec, str(source), "--json"]) == 2
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "schema version: missing" in captured.err
        assert snapshot() == ()
        assert constraint_snapshot() == initial_constraints
        assert index_snapshot() == initial_indexes

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    str(missing),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert captured.out == ""
        assert "neo4j-destination-missing.db" in captured.err
        assert snapshot() == ()
        assert constraint_snapshot() == initial_constraints
        assert index_snapshot() == initial_indexes

        assert (
            main(
                [
                    "merge",
                    spec,
                    str(source),
                    "--initialize-if-missing",
                    "--json",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        first = json.loads(captured.out)
        assert first["copied"] == 2
        assert first["existing"] == 0
        assert first["result_conflicts"] == 0
        assert first["meta_conflicts"] == 0

        with Neo4jStore(
            uri,
            (username, password),
            database=database,
            store_id=store_id,
            create=False,
        ) as destination:
            assert destination.exists(root_id)
            assert len(list(destination.walk(root_id))) == 2

        before_repeat = snapshot()
        constraints_before_repeat = constraint_snapshot()
        indexes_before_repeat = index_snapshot()
        assert len(before_repeat) == 4
        assert len(constraints_before_repeat) == 2
        assert len(indexes_before_repeat) == 2
        assert all(row["state"] == "ONLINE" for row in indexes_before_repeat)
        assert main(["merge", spec, str(source), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        repeated = json.loads(captured.out)
        assert repeated["copied"] == 0
        assert repeated["existing"] == 2
        assert repeated["result_conflicts"] == 0
        assert repeated["meta_conflicts"] == 0
        assert snapshot() == before_repeat
        assert constraint_snapshot() == constraints_before_repeat
        assert index_snapshot() == indexes_before_repeat
        for secret in (uri, password):
            assert secret not in "".join(transcript)
    finally:
        with driver.session(database=database) as session:
            session.run(
                """
                MATCH (node)
                WHERE node.store_id = $store_id
                DELETE node
                """,
                store_id=store_id,
            ).consume()
        driver.close()


def test_cli_accepts_configured_neo4j_for_observation_export_and_merge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    uri = os.environ.get("POLLARD_TEST_NEO4J_URI")
    password = os.environ.get("POLLARD_TEST_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("Neo4j is not configured")
    from neo4j import GraphDatabase

    username = os.environ.get("POLLARD_TEST_NEO4J_USER", "neo4j")
    database = os.environ.get("POLLARD_TEST_NEO4J_DATABASE", "neo4j")
    unique = uuid.uuid4().hex
    store_id = f"cli-{unique}"
    uri_variable = "POLLARD_CLI_TEST_NEO4J_URI"
    user_variable = "POLLARD_CLI_TEST_NEO4J_USER"
    password_variable = "POLLARD_CLI_TEST_NEO4J_PASSWORD"
    monkeypatch.setenv(uri_variable, uri)
    monkeypatch.setenv(user_variable, username)
    monkeypatch.setenv(password_variable, password)
    driver = GraphDatabase.driver(uri, auth=(username, password))

    try:
        with Neo4jStore(
            uri,
            (username, password),
            database=database,
            store_id=store_id,
        ) as store:
            run = Runtime(store).run(f"cli-neo4j-{unique}")
            run.model_call(
                {"model": "mock-1", "prompt": "remote CLI acceptance"},
                fn=lambda _payload: {
                    "text": "remote stored result",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            )
            root_id = run.root_id

        spec = (
            f"neo4j-env:{uri_variable}"
            f"?user-env={user_variable}"
            f"&password-env={password_variable}"
            f"&database={quote(database, safe='')}#{quote(store_id, safe='')}"
        )
        label = (
            f"neo4j-env:{uri_variable}"
            f"?database={quote(database, safe='')}#{quote(store_id, safe='')}"
        )
        transcript: list[str] = []

        assert main(["show", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["root_id"] == root_id

        assert main(["report", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["spent"]["tokens"] == 3

        assert main(["verify", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        verified = json.loads(captured.out)
        assert verified["ok"] is True
        assert verified["roots"] == [root_id]

        assert main(["seal", spec, root_id, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        sealed = json.loads(captured.out)

        export_path = tmp_path / "neo4j-acceptance.json"
        assert main(["export", spec, root_id, str(export_path), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        exported = json.loads(captured.out)
        assert exported["digest"] == sealed["digest"]
        assert exported["nodes"] == 2

        assert main(["runs", spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        listed = json.loads(captured.out)
        assert listed["runs"][0]["root_id"] == root_id
        assert listed["runs"][0]["store"] == label

        imported_db = tmp_path / "neo4j-imported.db"
        assert main(["import", str(export_path), str(imported_db), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        assert json.loads(captured.out)["imported"] == 2

        assert main(["verify", str(imported_db), "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        imported_verification = json.loads(captured.out)
        assert imported_verification["ok"] is True
        assert imported_verification["roots"] == [root_id]

        merged_db = tmp_path / "neo4j-merged.db"
        assert main(["merge", str(merged_db), spec, "--json"]) == 0
        captured = capsys.readouterr()
        transcript.extend((captured.out, captured.err))
        merged = json.loads(captured.out)
        assert merged["copied"] == 2
        assert merged["sources"][0]["source"] == label
        with SQLiteStore(merged_db) as merged_store:
            assert merged_store.exists(root_id)

        exposed = "".join(transcript)
        for secret in (
            uri,
            password,
            user_variable,
            password_variable,
        ):
            assert secret not in exposed
    finally:
        try:
            with driver.session(database=database) as session:
                session.run(
                    """
                    MATCH (node)
                    WHERE node.store_id = $store_id
                    DETACH DELETE node
                    """,
                    store_id=store_id,
                ).consume()
        finally:
            driver.close()


def _golden_tree() -> tuple[MemoryStore, Node]:
    store = MemoryStore()
    root = Node.make(kind=NodeKind.ROOT, parent=None, payload={"run": "golden"})
    store.put(root)
    model = Node.make(
        kind=NodeKind.MODEL_CALL,
        parent=root.id,
        payload={"model": "mock-1", "prompt": "private prompt"},
        result={"text": "private result"},
        meta={"charges": {"steps": 1, "tokens": 3}},
    )
    store.put(model)
    pruned = Node.make(
        kind=NodeKind.NOTE,
        parent=root.id,
        payload={"label": "alternate"},
        meta={"pruned": True},
    )
    store.put(pruned)
    refusal = Node.make(
        kind=NodeKind.REFUSAL,
        parent=pruned.id,
        payload={"reason": "budget", "meter": "tokens"},
    )
    store.put(refusal)
    return store, root
