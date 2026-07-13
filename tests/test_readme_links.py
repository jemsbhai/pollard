import re
from pathlib import Path


def test_readme_markdown_links_use_absolute_urls() -> None:
    readmes = sorted(
        path
        for path in Path(".").glob("**/README.md")
        if not any(part.startswith(".") for part in path.parts)
    )
    assert readmes
    for path in readmes:
        readme = path.read_text(encoding="utf-8")
        targets = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", readme)
        targets.extend(re.findall(r"^\[[^\]]+\]:\s+(\S+)", readme, flags=re.MULTILINE))
        targets.extend(re.findall(r'(?:href|src)="([^"]+)"', readme))
        assert all(target.startswith("https://") for target in targets), (path, targets)
