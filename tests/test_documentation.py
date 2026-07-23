import ast
import subprocess
import sys
from pathlib import Path

import pollard

ROOT = Path(__file__).resolve().parents[1]


def test_github_workflows_cannot_publish() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    workflows = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    assert workflows
    forbidden = (
        "gh-action-pypi-publish",
        "softprops/action-gh-release",
        "pypi",
        "publish",
        "twine upload",
        "upload.pypi.org",
        "id-token:",
        "gh release create",
        "sigstore",
    )
    for workflow in workflows:
        source = workflow.read_text(encoding="utf-8").lower()
        assert not any(term in source for term in forbidden), workflow
        assert "permissions:\n  contents: read" in source, workflow
        assert "contents: write" not in source, workflow


def test_release_runbook_declares_local_only_production_upload() -> None:
    runbook = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    required = (
        "maintainer-controlled local environment",
        "does not use TestPyPI",
        "python -m twine check --strict",
        "Get-FileHash -Algorithm SHA256",
        "python -m twine upload --non-interactive --repository pypi",
        "python -m pip install --no-cache-dir",
        "python examples\\exp_006_verify.py",
    )
    assert all(text in runbook for text in required)


def test_docs_index_names_every_top_level_document() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    documents = sorted((ROOT / "docs").glob("*.md"))
    for document in documents:
        if document.name != "README.md":
            assert f"/docs/{document.name}" in index, document


def test_api_reference_names_every_root_export() -> None:
    reference = (ROOT / "docs" / "api-reference.md").read_text(encoding="utf-8")
    for name in pollard.__all__:
        assert name in reference, name


def test_recipe_index_and_offline_help_cover_every_recipe() -> None:
    recipe_dir = ROOT / "docs" / "recipes"
    index = (recipe_dir / "README.md").read_text(encoding="utf-8")
    scripts = sorted(recipe_dir.glob("*.py"))
    assert len(scripts) == 8
    for script in scripts:
        assert script.name in index
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        assert ast.get_docstring(tree)
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "usage:" in result.stdout


def test_example_index_names_every_python_file() -> None:
    index = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    for script in sorted((ROOT / "examples").glob("*.py")):
        assert script.name in index, script


def test_distributed_store_guide_covers_operational_contract() -> None:
    guide = (ROOT / "docs" / "distributed-stores.md").read_text(encoding="utf-8")
    required = (
        "examples/09_distributed_stores.py",
        "POLLARD_PG_DSN",
        "POLLARD_REDIS_URL",
        "POLLARD_MONGODB_URI",
        "POLLARD_NEO4J_URI",
        "POLLARD_KAFKA_BOOTSTRAP",
        "POLLARD_KAFKA_TOPIC",
        "## Lifecycle, Reconnect, And Uncertain Outcomes",
        "ReservationUncertain",
        "SettlementUncertain",
        "ReservationLeaseLost",
        "## Logical Isolation And Authorization",
        "## Move Existing Recordings",
        "merge(destination, source)",
        "Redis Cluster",
        "Sentinel topology discovery",
        "cleanup.policy=delete",
        "retention.ms=-1",
        "external seal",
    )
    assert all(text in guide for text in required)


def test_kafka_retention_boundary_is_consistent_across_guides() -> None:
    governance = (ROOT / "docs" / "data-governance.md").read_text(encoding="utf-8")
    distributed = (ROOT / "docs" / "distributed-stores.md").read_text(
        encoding="utf-8"
    )
    assert "dedicated-topic level" in governance
    assert "selective" in governance and "node erasure" in governance
    assert "KafkaStore has no physical GC method" in distributed
