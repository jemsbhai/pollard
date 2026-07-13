import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from pollard import MemoryStore, Runtime, SQLiteStore
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
