import re
from pathlib import Path


def test_readme_markdown_links_use_absolute_urls() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    targets = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", readme)
    targets.extend(re.findall(r"^\[[^\]]+\]:\s+(\S+)", readme, flags=re.MULTILINE))
    targets.extend(re.findall(r'(?:href|src)="([^"]+)"', readme))
    assert targets
    assert all(target.startswith("https://") for target in targets), targets
