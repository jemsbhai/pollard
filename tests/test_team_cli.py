import json
from pathlib import Path

from pollard import ResultReference, Runtime, Team, record_dependency
from pollard.cli import main
from pollard.meters import StepMeter
from pollard.stores.sqlite import SQLiteStore


def test_team_inspection_is_read_only_and_omits_prompt_content(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "team.db"
    with SQLiteStore(path) as store:
        team = Team(Runtime(store, meters=[StepMeter()]), "test")
        writer = team.agent("writer", task_id="draft")
        draft = writer.run.model_call({"prompt": "PRIVATE INPUT"}, fn=lambda _: {"text": "PRIVATE"})
        reviewer = team.agent("reviewer", task_id="review")
        record_dependency(reviewer.run, [ResultReference.from_node(draft)])
        root = team.root_id
        count = len(list(store.walk(root)))
    assert main(["team-report", str(path), root, "--json"]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    report = json.loads(output)
    assert report["totals"]["governed_calls"] == 1
    assert main(["verify-dependencies", str(path), root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["team-report", str(path), root]) == 0
    assert "writer / draft: 1 calls" in capsys.readouterr().out
    assert main(["verify-dependencies", str(path), root]) == 0
    assert "Dependencies: ok" in capsys.readouterr().out
    with SQLiteStore(path, read_only=True) as store:
        assert len(list(store.walk(root))) == count


def test_team_inspection_refuses_missing_database(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "missing.db"
    assert main(["team-report", str(path), "0" * 64, "--json"]) == 2
    assert "pollard:" in capsys.readouterr().err
    assert not path.exists()
