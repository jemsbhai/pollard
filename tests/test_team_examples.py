"""Check observable outcomes of the offline team walkthroughs."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_example(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / name)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["network_used"] is False
    assert result["hosted_model_spend_usd"] == 0
    return result


def test_team_example_replays_and_refuses_unauthorized_work() -> None:
    result = run_example("15_agent_team.py")
    assert result["replay_matched"] is True
    assert result["recorded"]["dispatches"] == {"model": 5, "read": 3, "publish": 0}
    assert result["replayed_dispatches"] == {"model": 0, "read": 0, "publish": 0}
    assert result["recorded"]["denied_tool"] is True
    assert result["recorded"]["nested_limit_stopped"] is True
    assert result["recorded"]["text"] == "north: 12; south: 7; west: 9"


def test_independent_team_connections_share_exact_cap_and_restore_cursor() -> None:
    result = run_example("16_distributed_team.py")
    assert result["accepted"] == result["callable_executions"] == 3
    assert result["refused"] == 1
    assert result["attached_agents"] == result["unique_start_cursors"] == 4
    assert result["checkpoints_restored"] is True


def test_comparison_has_same_outcome_and_exposes_added_calls() -> None:
    result = run_example("17_team_comparison.py")
    single, three = result["results"]
    assert (
        single["quality"]
        == three["quality"]
        == {
            "expected_findings": 3,
            "correct_findings": 3,
            "exact_match": True,
        }
    )
    assert single["findings"] == three["findings"]
    assert single["report"]["totals"]["model_calls"] == 1
    assert three["report"]["totals"]["model_calls"] == 3
    assert single["report"]["totals"]["charges"]["tokens"] == 23
    assert three["report"]["totals"]["charges"]["tokens"] == 33
    assert all(row["local_elapsed_seconds"] >= 0 for row in result["results"])


def test_durable_approval_survives_worker_reconstruction() -> None:
    result = run_example("18_durable_approval.py")
    assert result["paused_before_approval"] is True
    assert result["worker_reconstructed"] is True
    assert result["handler_calls"] == result["side_effect_count"] == 1
    assert result["operation_id"] == "demo-operation-001"
    assert result["receipt"] == "receipt-1"
    assert result["report"]["totals"]["tool_calls"] == 1
    assert result["report"]["totals"]["refusals"] == 1
    assert result["report"]["totals"]["charges"]["steps"] == 1


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Create a team and give each task a cursor", "Checked: The measured count is 12."),
        ("Share a provider allowance across independent teams", "allowance is exhausted"),
        ("Run workers in separate processes", "['task-0', 'task-1']"),
        ("Use async execution or an existing framework", "['task-0', 'task-1']"),
    ],
)
def test_complete_team_guide_programs_run(heading: str, expected: str, tmp_path: Path) -> None:
    guide = (ROOT / "docs" / "teams.md").read_text(encoding="utf-8")
    section = guide.split(f"## {heading}\n", maxsplit=1)[1]
    program = section.split("```python\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]
    script = tmp_path / "team_guide.py"
    script.write_text(program + "\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert expected in completed.stdout
