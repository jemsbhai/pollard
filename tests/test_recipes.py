import subprocess
import sys
from pathlib import Path

from pollard import SQLiteStore, verify

ROOT = Path(__file__).parents[1]
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


def test_recipe_scripts_compile() -> None:
    recipe_dir = ROOT / "docs" / "recipes"
    scripts = sorted(recipe_dir.glob("*.py"))
    assert {script.name for script in scripts} == EXPECTED_RECIPES
    for script in scripts:
        compile(script.read_text(encoding="utf-8"), str(script), "exec")


def test_mcp_demo_server_compiles() -> None:
    script = ROOT / "examples" / "mcp_demo_server.py"
    compile(script.read_text(encoding="utf-8"), str(script), "exec")


def test_mcp_recipe_runs_end_to_end_without_network(tmp_path: Path) -> None:
    database = tmp_path / "mcp-recipe.db"
    result = subprocess.run(
        [
            sys.executable,
            "docs/recipes/mcp_registry.py",
            "--stdio",
            "--server-arg",
            "examples/mcp_demo_server.py",
            "--database",
            str(database),
            sys.executable,
            "search",
            "-",
        ],
        cwd=ROOT,
        input='\xef\xbb\xbf{"query":"pollard"}',
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Pollard governed execution trees" in result.stdout
    with SQLiteStore(database) as store:
        roots = store.roots()
        assert len(roots) == 1
        assert verify(store, roots[0]).ok
