"""Live comparison of a recorded model result with a new provider observation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from ._canon import IdentityValue, canonical_bytes

REPLAY_CONTRACT_FORMAT = "pollard/replay-contract/v1"
REVALIDATION_FORMAT = "pollard/revalidation/v1"
MAX_DIFFERENCE_PATHS = 100


@dataclass(frozen=True)
class ReplayContract:
    """Caller-declared execution fingerprint for recording and revalidation."""

    provider: str
    model_revision: str | None = None
    api_version: str | None = None
    adapter: str | None = None
    adapter_version: str | None = None
    sdk: str | None = None
    sdk_version: str | None = None
    application_revision: str | None = None
    environment: dict[str, IdentityValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty("provider", self.provider)
        for name in (
            "model_revision",
            "api_version",
            "adapter",
            "adapter_version",
            "sdk",
            "sdk_version",
            "application_revision",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_nonempty(name, value)
        if not isinstance(self.environment, dict):
            raise TypeError("environment must be an object")
        canonical_bytes(self.environment)
        object.__setattr__(self, "environment", deepcopy(self.environment))

    def to_dict(self) -> dict[str, IdentityValue]:
        document: dict[str, IdentityValue] = {
            "format": REPLAY_CONTRACT_FORMAT,
            "provider": self.provider,
        }
        for name in (
            "model_revision",
            "api_version",
            "adapter",
            "adapter_version",
            "sdk",
            "sdk_version",
            "application_revision",
        ):
            value = getattr(self, name)
            if value is not None:
                document[name] = value
        if self.environment:
            document["environment"] = deepcopy(self.environment)
        return document

    def bind(self, payload: dict[str, IdentityValue]) -> dict[str, IdentityValue]:
        """Return a payload whose identity includes this execution fingerprint."""

        canonical_bytes(payload)
        bound = deepcopy(payload)
        reserved = bound.get("_pollard")
        if reserved is None:
            metadata: dict[str, IdentityValue] = {}
        elif isinstance(reserved, dict):
            metadata = deepcopy(reserved)
        else:
            raise TypeError("payload _pollard field must be an object")
        contract = self.to_dict()
        existing = metadata.get("replay_contract")
        if existing is not None and existing != contract:
            raise ValueError("payload is already bound to a different replay contract")
        metadata["replay_contract"] = contract
        bound["_pollard"] = metadata
        return bound


@dataclass(frozen=True)
class RevalidationComparison:
    """Value-free comparator outcome safe to retain in an audit node."""

    matched: bool
    difference_paths: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.matched, bool):
            raise TypeError("matched must be bool")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be bool")
        paths = tuple(self.difference_paths)
        object.__setattr__(self, "difference_paths", paths)
        if len(paths) > MAX_DIFFERENCE_PATHS:
            raise ValueError(
                f"difference paths cannot exceed {MAX_DIFFERENCE_PATHS} entries"
            )
        if any(
            not isinstance(path, str) or not path.startswith("/")
            for path in paths
        ):
            raise ValueError("difference paths must be JSON pointers")
        if self.matched and (paths or self.truncated):
            raise ValueError("a matched comparison cannot contain differences")

    def to_dict(self) -> dict[str, IdentityValue]:
        return {
            "matched": self.matched,
            "difference_paths": list(self.difference_paths),
            "truncated": self.truncated,
        }


class RevalidationComparator(Protocol):
    """Protocol for value-free live-result comparisons."""

    name: str

    def compare(
        self,
        recorded: dict[str, Any],
        live: dict[str, Any],
    ) -> RevalidationComparison: ...


class ExactResultComparator:
    """Compare the complete normalized result, including usage and provider fields."""

    name = "exact-result/v1"

    def compare(
        self,
        recorded: dict[str, Any],
        live: dict[str, Any],
    ) -> RevalidationComparison:
        paths, truncated = _difference_paths(recorded, live)
        return RevalidationComparison(
            matched=not paths and not truncated,
            difference_paths=paths,
            truncated=truncated,
        )


class NormalizedModelComparator:
    """Compare stable model semantics while excluding volatile accounting fields."""

    name = "normalized-model/v1"

    def compare(
        self,
        recorded: dict[str, Any],
        live: dict[str, Any],
    ) -> RevalidationComparison:
        recorded_semantics = _model_semantics(recorded)
        live_semantics = _model_semantics(live)
        paths, truncated = _difference_paths(recorded_semantics, live_semantics)
        return RevalidationComparison(
            matched=not paths and not truncated,
            difference_paths=paths,
            truncated=truncated,
        )


@dataclass(frozen=True)
class RevalidationReport:
    """Structured outcome of one explicit live revalidation."""

    observation_id: str
    recorded_node_id: str
    live_node_id: str
    evidence_node_id: str
    comparator: str
    matched: bool
    exact_match: bool
    recorded_result_digest: str
    live_result_digest: str
    difference_paths: tuple[str, ...]
    differences_truncated: bool
    recorded_contract: dict[str, IdentityValue] | None
    live_contract: dict[str, IdentityValue]
    charges: dict[str, int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "recorded_node_id": self.recorded_node_id,
            "live_node_id": self.live_node_id,
            "evidence_node_id": self.evidence_node_id,
            "comparator": self.comparator,
            "matched": self.matched,
            "exact_match": self.exact_match,
            "recorded_result_digest": self.recorded_result_digest,
            "live_result_digest": self.live_result_digest,
            "difference_paths": list(self.difference_paths),
            "differences_truncated": self.differences_truncated,
            "recorded_contract": deepcopy(self.recorded_contract),
            "live_contract": deepcopy(self.live_contract),
            "charges": dict(self.charges),
        }


def make_revalidation_payload(
    payload: dict[str, IdentityValue],
    *,
    observation_id: str,
    recorded_node_id: str,
    recorded_result_digest: str,
    contract: ReplayContract,
    comparator_name: str,
) -> dict[str, IdentityValue]:
    """Add a provider-stripped identity marker for one live observation."""

    _require_nonempty("observation_id", observation_id)
    _require_nonempty("comparator name", comparator_name)
    marked = deepcopy(payload)
    reserved = marked.get("_pollard")
    if reserved is None:
        metadata: dict[str, IdentityValue] = {}
    elif isinstance(reserved, dict):
        metadata = deepcopy(reserved)
    else:
        raise TypeError("payload _pollard field must be an object")
    if "revalidation" in metadata:
        raise ValueError("payload already contains reserved _pollard.revalidation metadata")
    metadata["revalidation"] = {
        "format": REVALIDATION_FORMAT,
        "observation_id": observation_id,
        "recorded_node_id": recorded_node_id,
        "recorded_result_digest": recorded_result_digest,
        "live_contract": contract.to_dict(),
        "comparator": comparator_name,
    }
    marked["_pollard"] = metadata
    canonical_bytes(marked)
    return marked


def make_revalidation_evidence(
    *,
    observation_id: str,
    recorded_node_id: str,
    live_node_id: str,
    recorded_result_digest: str,
    live_result_digest: str,
    comparator_name: str,
    comparison: RevalidationComparison,
    exact_match: bool,
    recorded_contract: dict[str, IdentityValue] | None,
    live_contract: dict[str, IdentityValue],
) -> dict[str, IdentityValue]:
    """Create the immutable, value-free payload retained after comparison."""

    payload: dict[str, IdentityValue] = {
        "event": "model_revalidation",
        "format": REVALIDATION_FORMAT,
        "observation_id": observation_id,
        "recorded_node_id": recorded_node_id,
        "live_node_id": live_node_id,
        "recorded_result_digest": recorded_result_digest,
        "live_result_digest": live_result_digest,
        "comparator": comparator_name,
        "comparison": comparison.to_dict(),
        "exact_match": exact_match,
        "live_contract": deepcopy(live_contract),
    }
    if recorded_contract is not None:
        payload["recorded_contract"] = deepcopy(recorded_contract)
    canonical_bytes(payload)
    return payload


def make_revalidation_failure_evidence(
    *,
    observation_id: str,
    recorded_node_id: str,
    live_node_id: str,
    recorded_result_digest: str,
    live_result_digest: str,
    comparator_name: str,
    error: BaseException,
) -> dict[str, IdentityValue]:
    """Create a content-free note when comparison itself cannot complete."""

    payload: dict[str, IdentityValue] = {
        "event": "model_revalidation_comparison_failed",
        "format": REVALIDATION_FORMAT,
        "observation_id": observation_id,
        "recorded_node_id": recorded_node_id,
        "live_node_id": live_node_id,
        "recorded_result_digest": recorded_result_digest,
        "live_result_digest": live_result_digest,
        "comparator": comparator_name,
        "error_type": type(error).__name__,
    }
    canonical_bytes(payload)
    return payload


def extract_replay_contract(
    payload: dict[str, IdentityValue],
) -> dict[str, IdentityValue] | None:
    """Read a valid bound replay contract from an identity payload."""

    reserved = payload.get("_pollard")
    if not isinstance(reserved, dict) or "replay_contract" not in reserved:
        return None
    contract = reserved["replay_contract"]
    if not isinstance(contract, dict):
        raise ValueError("recorded _pollard.replay_contract must be an object")
    canonical_bytes(contract)
    return deepcopy(contract)


def _model_semantics(result: dict[str, Any]) -> Any:
    semantic_names = ("text", "tool_calls", "refusal", "structured_output")
    if any(name in result for name in semantic_names):
        projected: dict[str, Any] = {}
        for name in semantic_names:
            if name not in result:
                continue
            value = result[name]
            projected[name] = _normalize_tool_calls(value) if name == "tool_calls" else value
        return projected
    ignored = {"usage", "provider_usage", "chunks"}
    return {key: value for key, value in result.items() if key not in ignored}


def _normalize_tool_calls(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [_normalize_tool_call(item) for item in value]


def _normalize_tool_call(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized: dict[Any, Any] = {}
    for key, item in value.items():
        if key in {"id", "call_id", "toolUseId", "index"}:
            continue
        if key == "function" and isinstance(item, dict):
            function = dict(item)
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                function["arguments"] = _parse_json(arguments)
            normalized[key] = function
        elif key in {"arguments", "input_json"} and isinstance(item, str):
            normalized[key] = _parse_json(item)
        else:
            normalized[key] = item
    return normalized


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _difference_paths(
    recorded: Any,
    live: Any,
    *,
    limit: int = MAX_DIFFERENCE_PATHS,
) -> tuple[tuple[str, ...], bool]:
    paths: list[str] = []
    truncated = False

    def add(path: str) -> None:
        nonlocal truncated
        if len(paths) >= limit:
            truncated = True
            return
        paths.append(path or "/")

    def visit(left: Any, right: Any, path: str) -> None:
        nonlocal truncated
        if truncated:
            return
        if type(left) is not type(right):
            add(path)
            return
        if isinstance(left, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys | right_keys, key=str):
                child = f"{path}/{_pointer_token(str(key))}"
                if key not in left_keys or key not in right_keys:
                    add(child)
                else:
                    visit(left[key], right[key], child)
            return
        if isinstance(left, list):
            common = min(len(left), len(right))
            for index in range(common):
                visit(left[index], right[index], f"{path}/{index}")
            for index in range(common, max(len(left), len(right))):
                add(f"{path}/{index}")
            return
        if left != right:
            add(path)

    visit(recorded, live, "")
    return tuple(paths), truncated


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _require_nonempty(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
