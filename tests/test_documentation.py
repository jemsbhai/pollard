import ast
import os
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest

import pollard

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RECIPES = {
    "anthropic_tool_loop.py",
    "azure_openai.py",
    "bedrock_converse.py",
    "langchain_incident_response.py",
    "langchain_support_rag.py",
    "langgraph_node.py",
    "litellm_cloud.py",
    "mcp_registry.py",
    "openai_tool_loop.py",
    "pydantic_ai_claim_triage.py",
    "pydantic_ai_wrap.py",
    "pydantic_refund_workflow.py",
}
FIRST_RUN_ROOT_ID = "fb4f2a23cc196e53f0fa800a71c025e0a9b7ac5890b83c4d9d1a0214175d9dd5"
FIRST_RUN_NODE_ID = "c4882b75addd9867f623049798e2c6cebc3d49daa80bd5a825c102cf0580fd30"


def _read_first_run_program() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    heading = "## 90-Second Credential-Free Start"
    assert heading in readme
    section = readme.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    assert FIRST_RUN_ROOT_ID in section
    assert FIRST_RUN_NODE_ID in section
    opening = "```python\n"
    assert opening in section
    program = section.split(opening, maxsplit=1)[1].split("\n```", maxsplit=1)[0]
    return f"{program.rstrip()}\n"


def test_readme_first_run_separates_posix_and_powershell_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## 90-Second Credential-Free Start", maxsplit=1)[1].split(
        "\n## ", maxsplit=1
    )[0]
    assert "On POSIX systems, including macOS:" in section
    assert "```sh\npython3 -m venv .venv\n.venv/bin/python -m pip install pollard" in section
    assert ".venv/bin/python first_run.py" in section
    assert "On Windows PowerShell:" in section
    assert "```powershell\npy -3 -m venv .venv" in section
    assert r".\.venv\Scripts\python.exe -m pip install pollard" in section
    assert r".\.venv\Scripts\python.exe first_run.py" in section
    assert "from examples" not in section
    assert "from openai" not in section


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheel_dir = tmp_path_factory.mktemp("wheel")
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
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


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
        "Author: Muntaser Syed",
        "info.author",
    )
    assert all(text in runbook for text in required)
    public_verification = runbook.split(
        "## 6. Verify public distribution", maxsplit=1
    )[1].split("## 7. Close the release", maxsplit=1)[0]
    assert 'python -m pip install -e ".[dev,estimate-openai]"' in public_verification
    assert "python -m pytest" in public_verification
    assert "python examples\\exp_006_verify.py" in public_verification


def test_built_wheel_exposes_consistent_version_and_author_metadata(
    built_wheel: Path, tmp_path: Path,
) -> None:
    wheel = built_wheel
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_path))

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project_version = tomllib.load(project_file)["project"]["version"]

    assert project_version == pollard.__version__
    assert metadata["Version"] == project_version
    assert metadata["Author"] == "Muntaser Syed"
    assert metadata["Maintainer-Email"] == "Muntaser Syed <jemsbhai@gmail.com>"

    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import importlib.metadata as metadata, sys; "
                "sys.path.insert(0, sys.argv[1]); "
                "import pollard; "
                "assert pollard.__version__ == sys.argv[2]; "
                "assert metadata.version('pollard') == sys.argv[2]"
            ),
            str(installed),
            project_version,
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_wheel_installed_first_run_works_outside_checkout(
    built_wheel: Path, tmp_path: Path
) -> None:
    venv_dir = tmp_path / "venv"
    run_dir = tmp_path / "consumer"
    run_dir.mkdir()
    script = run_dir / "first_run.py"
    script.write_text(
        (
            f"{_read_first_run_program()}\n"
            f'assert run.root_id == "{FIRST_RUN_ROOT_ID}"\n'
            f'assert node.id == "{FIRST_RUN_NODE_ID}"\n'
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    venv_python = venv_dir / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(built_wheel),
        ],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
    )

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(venv_python), "-I", script.name],
        cwd=run_dir,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "offline reply for hello",
        "tokens: 6; steps: 1",
    ]
    assert result.stderr == ""


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
    assert {script.name for script in scripts} == EXPECTED_RECIPES
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
        "client_factory",
        "cleanup.policy=delete",
        "retention.ms=-1",
        "external seal",
    )
    assert all(text in guide for text in required)


def test_remote_store_guides_cover_each_production_lifecycle() -> None:
    guides = {
        "redis-operations.md": ("Sentinel", "noeviction", "WATCH"),
        "mongodb-operations.md": ("replica set", "majority", "timeoutMS"),
        "neo4j-operations.md": ("neo4j+s://", "Enterprise", "routing"),
        "kafka-operations.md": (
            "min.insync.replicas",
            "unclean leader election",
            "offset zero",
        ),
    }
    shared = (
        "## Monitoring",
        "Credential Rotation",
        "## Production Acceptance",
        "Backup",
        "seal",
    )
    for name, backend_terms in guides.items():
        guide = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert all(term in guide for term in (*shared, *backend_terms)), name


def test_kafka_retention_boundary_is_consistent_across_guides() -> None:
    governance = (ROOT / "docs" / "data-governance.md").read_text(encoding="utf-8")
    distributed = (ROOT / "docs" / "distributed-stores.md").read_text(
        encoding="utf-8"
    )
    assert "dedicated-topic level" in governance
    assert "selective" in governance and "node erasure" in governance
    assert "KafkaStore has no physical GC method" in distributed
