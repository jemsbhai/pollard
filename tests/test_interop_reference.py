"""Keep frozen native behavior checks separate from release metadata updates."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    generator_path = ROOT / "interop" / "generate_npm_parity.py"
    if not generator_path.is_file():
        pytest.skip("native parity generator is source-only and excluded from the Python sdist")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    spec = importlib.util.spec_from_file_location(
        "pollard_npm_parity_generator", generator_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "__version__", "1.6.1")
    monkeypatch.setattr(sys, "argv", ["generate_npm_parity.py", "--check"])
    document = module.generate()
    document["python_release"] = "1.6.0"
    path = tmp_path / "packages" / "npm" / "test" / "python-parity.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return module


def test_check_preserves_frozen_reference_when_only_patch_version_changes(
    generator: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    path = generator.ROOT / "packages/npm/test/python-parity.json"
    original = path.read_bytes()
    assert generator.main() == 0
    assert path.read_bytes() == original
    output = capsys.readouterr().out
    assert "running Python 1.6.1" in output
    assert "recorded Python 1.6.0 reference" in output


def test_check_rejects_changed_behavior_despite_patch_version_difference(
    generator: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    path = generator.ROOT / "packages/npm/test/python-parity.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["providers"][0]["expected"]["text"] = "different output"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    original = path.read_bytes()
    assert generator.main() == 1
    assert path.read_bytes() == original
    assert "differs from the Python reference" in capsys.readouterr().err


def test_explicit_regeneration_updates_reference_release(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["generate_npm_parity.py"])
    assert generator.main() == 0
    path = generator.ROOT / "packages/npm/test/python-parity.json"
    assert json.loads(path.read_text(encoding="utf-8"))["python_release"] == "1.6.1"
