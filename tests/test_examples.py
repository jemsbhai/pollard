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


@pytest.mark.parametrize(
    "script",
    [
        "examples/exp_001_local_model.py",
        "examples/exp_004_storage.py",
        "examples/exp_005_contention.py",
    ],
)
def test_formal_experiment_runner_help_is_offline(script: str) -> None:
    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "usage:" in result.stdout


def test_distributed_store_example_help_is_offline() -> None:
    result = subprocess.run(
        [sys.executable, "examples/09_distributed_stores.py", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "postgresql" in result.stdout
    assert "kafka" in result.stdout
