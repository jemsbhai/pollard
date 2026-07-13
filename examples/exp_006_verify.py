"""Create and verify the combined, fully offline EXP-006 evidence manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from _exp006_common import sha256_file, write_json
from exp_006_code_fix import WORKSPACES
from exp_006_code_fix import _registry as code_registry
from exp_006_research import _load_documents
from exp_006_research import _registry as research_registry

from pollard import ActionSpec, Registry, Runtime, SQLiteStore, seal, verify
from pollard.meters import StepMeter

CASE_PATHS = {
    "EXP-006A": Path("evidence/EXP-006/research"),
    "EXP-006B": Path("evidence/EXP-006/code-fix"),
    "EXP-006C": Path("evidence/EXP-006/mcp-household"),
}
CASE_INPUTS = {
    "EXP-006A": [
        Path(f"evidence/EXP-006/inputs/research/DOC-00{index}.md") for index in range(1, 4)
    ],
    "EXP-006B": [
        Path("evidence/EXP-006/inputs/code-fix/repo/calculator.py"),
        Path("evidence/EXP-006/inputs/code-fix/repo/test_calculator.py"),
    ],
    "EXP-006C": [
        Path("evidence/EXP-006/inputs/mcp/servers/catalog_server.py"),
        Path("evidence/EXP-006/inputs/mcp/servers/math_server.py"),
        Path("evidence/EXP-006/inputs/mcp/servers/policy_server.py"),
    ],
}
EXTRA_ARTIFACTS = {
    "EXP-006A": [],
    "EXP-006B": [Path("fixed/calculator.py"), Path("test-result.json")],
    "EXP-006C": [],
}
WINDOWS_PATH = re.compile(r"[A-Za-z]:\\(?:Users|Documents and Settings)\\", re.IGNORECASE)
SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|api[_-]?key\s*[:=])", re.IGNORECASE)
REMOTE_ASSET = re.compile(
    r"(?:src|href)\s*=\s*[\"']https?://|url\(\s*[\"']?https?://",
    re.IGNORECASE,
)


def _dummy_tool(_args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("strict replay unexpectedly invoked a tool handler")


def _dummy_model(_payload: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("strict replay unexpectedly invoked the model adapter")


def _mcp_registry() -> Registry:
    catalog_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    math_schema = {
        "type": "object",
        "properties": {"prices_cents": {"type": "array", "items": {"type": "integer"}}},
        "required": ["prices_cents"],
        "additionalProperties": False,
    }
    policy_schema = {
        "type": "object",
        "properties": {
            "total_cents": {"type": "integer"},
            "limit_cents": {"type": "integer"},
        },
        "required": ["total_cents", "limit_cents"],
        "additionalProperties": False,
    }
    return Registry(
        [
            ActionSpec(
                "lookup_household_items",
                "mcp",
                "Search the pinned household catalog for planning candidates.",
                catalog_schema,
                True,
                _dummy_tool,
            ),
            ActionSpec(
                "sum_prices",
                "mcp",
                "Sum a list of integer prices in cents without floating-point rounding.",
                math_schema,
                True,
                _dummy_tool,
            ),
            ActionSpec(
                "check_household_budget",
                "mcp",
                ("Approve a household order only when its integer total is within the limit."),
                policy_schema,
                True,
                _dummy_tool,
            ),
        ]
    )


def _registries(repo: Path) -> dict[str, Registry]:
    documents = _load_documents(repo / "evidence/EXP-006/inputs/research")
    dummy_workspaces = {name: repo for name in WORKSPACES}
    return {
        "EXP-006A": research_registry(documents),
        "EXP-006B": code_registry(dummy_workspaces, repo),
        "EXP-006C": _mcp_registry(),
    }


def _artifact_paths(case_id: str) -> list[Path]:
    base = CASE_PATHS[case_id]
    return [
        base / "run.db",
        base / "seal.json",
        base / "tree.html",
        base / "outcome.json",
        *(base / relative for relative in EXTRA_ARTIFACTS[case_id]),
    ]


def create_manifest(repo: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case_id, relative in CASE_PATHS.items():
        outcome = _load_json(repo / relative / "outcome.json")
        artifact = _object(outcome.get("artifact"), f"{case_id} artifact")
        cases.append(
            {
                "id": case_id,
                "status": outcome.get("status"),
                "root_id": artifact.get("root_id"),
                "seal_digest": artifact.get("seal_digest"),
                "node_count": artifact.get("node_count"),
                "registry_digest": outcome.get("registry_digest"),
                "provider_spend_usd": outcome.get("provider_spend_usd"),
                "inputs": {
                    path.as_posix(): sha256_file(repo / path) for path in CASE_INPUTS[case_id]
                },
                "artifacts": {
                    path.as_posix(): sha256_file(repo / path) for path in _artifact_paths(case_id)
                },
            }
        )
    return {
        "schema": "pollard/exp-006-manifest/v1",
        "experiment": "EXP-006",
        "release_target": "1.0.0",
        "recording_network": "loopback-and-local-stdio-only",
        "replay_network": "none",
        "provider_spend_usd": sum(float(case["provider_spend_usd"]) for case in cases),
        "cases": cases,
    }


def verify_manifest(repo: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema") != "pollard/exp-006-manifest/v1":
        raise RuntimeError("unexpected EXP-006 manifest schema")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise RuntimeError("EXP-006 manifest must contain exactly three cases")
    registries = _registries(repo)
    results = []
    for raw_case in cases:
        case = _object(raw_case, "case")
        case_id = str(case["id"])
        if case_id not in CASE_PATHS:
            raise RuntimeError(f"unexpected case id: {case_id}")
        _verify_hashes(repo, _object(case.get("inputs"), f"{case_id} inputs"))
        _verify_hashes(repo, _object(case.get("artifacts"), f"{case_id} artifacts"))
        outcome = _load_json(repo / CASE_PATHS[case_id] / "outcome.json")
        if outcome.get("status") != "passed" or outcome.get("provider_spend_usd") != 0:
            raise RuntimeError(f"{case_id} does not record a zero-spend passing outcome")
        registry = registries[case_id]
        if registry.registry_digest != case.get("registry_digest"):
            raise RuntimeError(f"{case_id} offline registry digest mismatch")
        db_path = repo / CASE_PATHS[case_id] / "run.db"
        with SQLiteStore(db_path) as store:
            roots = store.roots()
            if roots != [case.get("root_id")]:
                raise RuntimeError(f"{case_id} root mismatch")
            root_id = roots[0]
            nodes = list(store.walk(root_id))
            for node in nodes:
                report = verify(store, node.id)
                if not report.ok:
                    raise RuntimeError(f"{case_id} verification failed at {node.id}")
                _scan_value(node.payload, f"{case_id}:{node.id}:payload")
                _scan_value(node.result, f"{case_id}:{node.id}:result")
            seal_report = seal(store, root_id)
            if seal_report.digest != case.get("seal_digest"):
                raise RuntimeError(f"{case_id} seal digest mismatch")
            if len(nodes) != case.get("node_count"):
                raise RuntimeError(f"{case_id} node count mismatch")
            paths = _leaf_paths(store, root_id)
            for path in paths:
                _strict_replay_path(store, registry, path)
        html_path = repo / CASE_PATHS[case_id] / "tree.html"
        html = html_path.read_text(encoding="utf-8")
        if REMOTE_ASSET.search(html):
            raise RuntimeError(f"{case_id} HTML contains a remote asset URL")
        results.append(
            {
                "id": case_id,
                "nodes": len(nodes),
                "paths_replayed": len(paths),
                "seal_digest": seal_report.digest,
            }
        )
    if manifest.get("provider_spend_usd") != 0:
        raise RuntimeError("combined EXP-006 provider spend is not zero")
    return {
        "ok": True,
        "network_used": False,
        "model_calls_executed": 0,
        "tool_calls_executed": 0,
        "cases": results,
    }


def _leaf_paths(store: SQLiteStore, root_id: str) -> list[list[Any]]:
    leaves = [node for node in store.walk(root_id) if not store.children(node.id)]
    paths: list[list[Any]] = []
    for leaf in leaves:
        path = []
        node = leaf
        while True:
            path.append(node)
            if node.parent is None:
                break
            node = store.get(node.parent)
        paths.append(list(reversed(path)))
    return paths


def _strict_replay_path(store: SQLiteStore, registry: Registry, path: list[Any]) -> None:
    root = path[0]
    label = root.payload.get("run")
    if not isinstance(label, str):
        raise RuntimeError("recorded root omitted its run label")
    replay = Runtime(store, registry=registry, meters=[StepMeter()], mode="replay").run(
        label,
        attempt=root.attempt,
    )
    for expected in path[1:]:
        if expected.kind == "note":
            actual = replay.note(expected.payload, attempt=expected.attempt)
        elif expected.kind == "model_call":
            actual = replay.model_call(
                expected.payload,
                fn=_dummy_model,
                attempt=expected.attempt,
            )
        elif expected.kind == "tool_call":
            tool = expected.payload.get("tool")
            args = expected.payload.get("args")
            version = expected.payload.get("version")
            if not isinstance(tool, str) or not isinstance(args, dict):
                raise RuntimeError("recorded tool node has an invalid payload")
            actual = replay.tool_call(
                tool,
                args,
                version=version if isinstance(version, str) else None,
                attempt=expected.attempt,
            )
        else:
            raise RuntimeError(f"unsupported strict-replay node kind: {expected.kind}")
        if actual.id != expected.id:
            raise RuntimeError(f"strict replay diverged at {expected.id}")


def _verify_hashes(repo: Path, values: dict[str, Any]) -> None:
    for relative, expected in values.items():
        path = repo / relative
        if not path.is_file():
            raise RuntimeError(f"manifest path is missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"manifest hash mismatch: {relative}")


def _scan_value(value: Any, location: str) -> None:
    if isinstance(value, str):
        if WINDOWS_PATH.search(value):
            raise RuntimeError(f"absolute user path leaked into {location}")
        if SECRET.search(value):
            raise RuntimeError(f"possible credential leaked into {location}")
        return
    if isinstance(value, dict):
        for item in value.values():
            _scan_value(item, location)
    elif isinstance(value, list):
        for item in value:
            _scan_value(item, location)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return _object(value, str(path))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("evidence/EXP-006/manifest.json"),
    )
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    manifest_path = args.manifest
    if not manifest_path.is_absolute():
        manifest_path = repo / manifest_path
    if args.write_manifest:
        write_json(manifest_path, create_manifest(repo))
    result = verify_manifest(repo, _load_json(manifest_path))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
