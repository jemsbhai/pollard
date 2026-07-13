from pathlib import Path


def test_recipe_scripts_compile() -> None:
    recipe_dir = Path(__file__).parents[1] / "docs" / "recipes"
    scripts = sorted(recipe_dir.glob("*.py"))
    assert len(scripts) == 5
    for script in scripts:
        compile(script.read_text(encoding="utf-8"), str(script), "exec")
