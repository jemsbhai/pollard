import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script",
    [
        "examples/01_governed_call.py",
        "examples/02_best_of_n.py",
        "examples/03_budget_stop.py",
        "examples/04_firewall.py",
        "examples/05_replay_ci.py",
        "examples/06_phase4_benchmarks.py",
        "examples/08_phase8_scaleout.py",
    ],
)
def test_example_script_runs_offline(script: str) -> None:
    result = subprocess.run(
        [sys.executable, script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip()
