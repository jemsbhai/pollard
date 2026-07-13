import json
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
    paths = sorted(EVIDENCE.glob("EXP-*/*.json"))
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


def test_readme_numeric_evidence_rows_name_their_experiment() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    evidence = readme.split("## Evidence", 1)[1].split("## 1.0 Stability Covenant", 1)[0]
    rows = [line for line in evidence.splitlines() if line.startswith("| EXP-")]
    assert len(rows) == 3
    assert all("EXP-" in row for row in rows)
