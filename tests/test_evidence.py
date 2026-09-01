import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

EVIDENCE = Path("evidence")


def _load(path: str) -> dict[str, Any]:
    return json.loads((EVIDENCE / path).read_text(encoding="utf-8"))


def test_formal_evidence_artifacts_pass_registered_protocols() -> None:
    exp001 = _load("EXP-001/local-model-result.json")
    assert (exp001["id"], exp001["status"]) == ("EXP-001", "passed")
    assert [row["branches"] for row in exp001["summary"]] == [2, 4, 8]
    assert all(row["seeds"] == 5 for row in exp001["summary"])
    assert all(row["output_digest_parity"] for row in exp001["summary"])

    exp004 = _load("EXP-004/result.json")
    assert (exp004["id"], exp004["status"]) == ("EXP-004", "passed")
    assert exp004["summary"]["all_node_ids_match"] is True
    assert [row["turns"] for row in exp004["summary"]["checkpoints"]] == [
        25,
        50,
        100,
        200,
    ]
    assert len(exp004["fits"]["interned"]) == 5
    assert len(exp004["fits"]["plain"]) == 5

    exp005 = _load("EXP-005/result.json")
    assert (exp005["id"], exp005["status"]) == ("EXP-005", "passed")
    assert len(exp005["targets"]) == 2
    assert all(target["passed"] for target in exp005["targets"])
    assert all(len(target["conditions"]) == 30 for target in exp005["targets"])
    assert sum(
        condition["rounds"]
        for target in exp005["targets"]
        for condition in target["conditions"]
    ) == 1_650


def test_formal_evidence_contains_no_common_secret_or_local_path_patterns() -> None:
    paths = sorted(EVIDENCE.glob("EXP-*/**/*.json"))
    assert paths
    forbidden = (
        "postgresql://",
        "c:\\users\\",
        "openai_api_key",
        "anthropic_api_key",
        "api_key",
        "password",
        "sk-",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(fragment in text for fragment in forbidden), path


def test_exp006_case_studies_pass_strict_offline_verification() -> None:
    manifest = _load("EXP-006/manifest.json")
    assert manifest["schema"] == "pollard/exp-006-manifest/v1"
    assert manifest["provider_spend_usd"] == 0
    assert [case["id"] for case in manifest["cases"]] == [
        "EXP-006A",
        "EXP-006B",
        "EXP-006C",
    ]
    assert sum(case["node_count"] for case in manifest["cases"]) == 49
    completed = subprocess.run(
        [sys.executable, "examples/exp_006_verify.py"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["network_used"] is False
    assert result["model_calls_executed"] == 0
    assert result["tool_calls_executed"] == 0
    assert sum(case["paths_replayed"] for case in result["cases"]) == 6


def test_exp007_runner_is_offline_and_binds_the_loaded_wheel(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=repository,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    venv_python = venv_dir / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(venv_python),
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    completed = subprocess.run(
        [
            str(venv_python),
            "-I",
            str(repository / "examples" / "exp_007_overhead.py"),
            "--steps",
            "4",
            "--samples",
            "3",
            "--warmups",
            "0",
            "--package-wheel",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repository,
    )
    result = json.loads(completed.stdout)
    assert result["schema"] == "pollard/exp-007-overhead/v1"
    assert result["status"] == "passed"
    assert result["network_used"] is False
    assert result["provider_spend_usd"] == 0
    assert len(result["cases"]) == 6

    provenance = result["provenance"]
    runner = Path(provenance["runner"])
    assert hashlib.sha256(runner.read_bytes()).hexdigest() == provenance[
        "runner_source_sha256"
    ]
    assert len(provenance["package_source_sha256"]) == 64
    assert provenance["package_source_file_count"] > 0
    runner_repository = provenance["runner_repository"]
    assert runner_repository["commit"] is None or len(runner_repository["commit"]) == 40
    assert runner_repository["root_matches_runner_repository"] in (True, False, None)
    assert runner_repository["dirty"] in (True, False, None)
    if runner_repository["root_matches_runner_repository"] is not True:
        assert runner_repository["commit"] is None
        assert runner_repository["dirty"] is None
    assert runner_repository["package_source_sha256"] == provenance[
        "package_source_sha256"
    ]
    assert runner_repository["package_source_file_count"] == provenance[
        "package_source_file_count"
    ]
    assert runner_repository["matches_loaded_package_sources"] is True
    loaded_distribution = provenance["loaded_distribution"]
    assert loaded_distribution["editable"] is not True
    assert loaded_distribution["archive_sha256"] == hashlib.sha256(
        wheel.read_bytes()
    ).hexdigest()
    assert loaded_distribution["matches_package_wheel"] is True
    assert loaded_distribution["loaded_from_distribution"] is True
    assert loaded_distribution["isolated_imports"] is True
    expected_publishable = (
        isinstance(runner_repository["commit"], str)
        and len(runner_repository["commit"]) == 40
        and runner_repository["root_matches_runner_repository"] is True
        and runner_repository["dirty"] is False
        and runner_repository["matches_loaded_package_sources"] is True
        and loaded_distribution["matches_package_wheel"] is True
        and loaded_distribution["loaded_from_distribution"] is True
        and loaded_distribution["isolated_imports"] is True
    )
    assert provenance["publishable"] is expected_publishable
    assert provenance["status"] == (
        "release-bound" if expected_publishable else "unbound"
    )
    bound_wheel = provenance["package_wheel"]
    assert bound_wheel["filename"] == wheel.name
    assert bound_wheel["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert bound_wheel["version"] == result["environment"]["pollard"]
    assert bound_wheel["package_source_sha256"] == provenance[
        "package_source_sha256"
    ]
    assert bound_wheel["package_source_file_count"] == provenance[
        "package_source_file_count"
    ]
    assert bound_wheel["matches_loaded_package_sources"] is True

    for case in result["cases"]:
        expected_calls = 4 if case["mode"] == "record" else 0
        expected_identity = (
            "fresh_chain" if case["mode"] == "record" else "recorded_chain_hit"
        )
        assert case["invariants_passed"] is True
        assert case["identity_condition"] == expected_identity
        assert case["expected_pollard_callable_calls_per_sample"] == expected_calls
        assert case["expected_node_count_per_sample"] == 5
        assert len(case["samples"]) == 3
        assert all(row["pollard_callable_calls"] == expected_calls for row in case["samples"])
        assert all(row["node_count"] == 5 for row in case["samples"])

    required = subprocess.run(
        [
            str(venv_python),
            "-I",
            str(repository / "examples" / "exp_007_overhead.py"),
            "--steps",
            "1",
            "--samples",
            "2",
            "--warmups",
            "0",
            "--package-wheel",
            str(wheel),
            "--require-publishable-provenance",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repository,
    )
    if expected_publishable:
        assert required.returncode == 0
        assert json.loads(required.stdout)["provenance"]["publishable"] is True
    else:
        assert required.returncode != 0
        assert "publishable provenance requires" in required.stderr

    shadowed_environment = os.environ.copy()
    shadowed_environment["PYTHONPATH"] = str(repository / "src")
    shadowed = subprocess.run(
        [
            str(venv_python),
            str(repository / "examples" / "exp_007_overhead.py"),
            "--steps",
            "1",
            "--samples",
            "2",
            "--warmups",
            "0",
            "--package-wheel",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=shadowed_environment,
        timeout=60,
        cwd=repository,
    )
    shadowed_result = json.loads(shadowed.stdout)
    assert shadowed_result["provenance"]["publishable"] is False
    assert shadowed_result["provenance"]["loaded_distribution"][
        "loaded_from_distribution"
    ] is False
    assert shadowed_result["provenance"]["loaded_distribution"][
        "isolated_imports"
    ] is False

    shadowed_required = subprocess.run(
        [
            str(venv_python),
            str(repository / "examples" / "exp_007_overhead.py"),
            "--steps",
            "1",
            "--samples",
            "2",
            "--warmups",
            "0",
            "--package-wheel",
            str(wheel),
            "--require-publishable-provenance",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=shadowed_environment,
        timeout=60,
        cwd=repository,
    )
    assert shadowed_required.returncode != 0
    assert "publishable provenance requires" in shadowed_required.stderr


def test_exp007_handles_missing_git_as_unbound_provenance() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PATH"] = ""
    environment["PYTHONPATH"] = str(repository_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "examples" / "exp_007_overhead.py"),
            "--steps",
            "1",
            "--samples",
            "2",
            "--warmups",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        cwd=repository_root,
    )
    result = json.loads(completed.stdout)
    repository = result["provenance"]["runner_repository"]

    assert result["status"] == "passed"
    assert result["provenance"]["status"] == "unbound"
    assert result["provenance"]["publishable"] is False
    assert repository["commit"] is None
    assert repository["dirty"] is None
    assert repository["root_matches_runner_repository"] is None
    assert repository["matches_loaded_package_sources"] is True


def test_readme_numeric_evidence_rows_name_their_experiment() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    evidence = readme.split("## Evidence", 1)[1].split("## 1.0 Stability Covenant", 1)[0]
    rows = [line for line in evidence.splitlines() if line.startswith("| EXP-")]
    assert len(rows) == 3
    assert all("EXP-" in row for row in rows)
