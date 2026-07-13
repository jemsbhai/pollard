"""Versioned action registry and zero-dependency schema validation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ._canon import IdentityValue, canonical_bytes
from .errors import UnsupportedSchema

ActionHandler = Callable[[dict[str, Any]], dict[str, Any]]

_SUPPORTED_KEYS = {"type", "properties", "required", "enum", "items", "additionalProperties"}
_SUPPORTED_TYPES = {"object", "string", "integer", "boolean", "array", "null"}


@dataclass(frozen=True)
class ActionSpec:
    name: str
    version: str
    description: str
    schema: dict[str, IdentityValue]
    side_effects: bool
    handler: ActionHandler | None = field(default=None, compare=False, repr=False)
    spec_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("action spec name cannot be empty")
        if not self.version:
            raise ValueError("action spec version cannot be empty")
        _check_schema(self.schema, f"schema for {self.name}")
        object.__setattr__(self, "spec_digest", _digest_spec(self))

    def validate_args(self, args: dict[str, IdentityValue]) -> str | None:
        try:
            canonical_bytes(args)
        except TypeError as exc:
            return str(exc)
        return _validate_value(args, self.schema, "$")


class Registry:
    """Frozen action registry."""

    def __init__(self, specs: list[ActionSpec] | tuple[ActionSpec, ...]) -> None:
        by_name: dict[str, ActionSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValueError(f"duplicate action spec name: {spec.name}")
            by_name[spec.name] = spec
        self._specs = dict(by_name)
        digest_values: list[IdentityValue] = []
        digest_values.extend(sorted(spec.spec_digest for spec in specs))
        self.registry_digest = hashlib.sha256(
            canonical_bytes({"spec_digests": digest_values})
        ).hexdigest()

    def get(self, name: str, version: str | None = None) -> ActionSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        if version is not None and version != spec.version:
            raise KeyError(f"{name}@{version}")
        return spec

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self._specs.values())


def _digest_spec(spec: ActionSpec) -> str:
    identity: dict[str, IdentityValue] = {
        "name": spec.name,
        "version": spec.version,
        "description": spec.description,
        "schema": spec.schema,
        "side_effects": spec.side_effects,
    }
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def _check_schema(schema: IdentityValue, path: str) -> None:
    if not isinstance(schema, dict):
        raise UnsupportedSchema(f"{path}: schema must be an object")
    unknown = set(schema) - _SUPPORTED_KEYS
    if unknown:
        raise UnsupportedSchema(f"{path}: unsupported keywords {sorted(unknown)}")
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or schema_type not in _SUPPORTED_TYPES
    ):
        raise UnsupportedSchema(f"{path}: unsupported type {schema_type!r}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise UnsupportedSchema(f"{path}.properties: must be an object")
        for name, child_schema in properties.items():
            _check_schema(child_schema, f"{path}.properties.{name}")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        raise UnsupportedSchema(f"{path}.required: must be a list of strings")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise UnsupportedSchema(f"{path}.additionalProperties: must be a boolean")
    canonical_bytes(schema)


def _validate_value(
    value: IdentityValue,
    schema: dict[str, IdentityValue],
    path: str,
) -> str | None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _matches_type(value, expected_type):
        return f"{path}: expected {expected_type}"
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path}: value not in enum"
    if expected_type == "object" or (
        expected_type is None and ("properties" in schema or "required" in schema)
    ):
        if not isinstance(value, dict):
            return f"{path}: expected object"
        required = schema.get("required", [])
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    return f"{path}: missing required property {name}"
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child_schema in properties.items():
                if name in value and isinstance(child_schema, dict):
                    finding = _validate_value(value[name], child_schema, f"{path}.{name}")
                    if finding is not None:
                        return finding
            if schema.get("additionalProperties") is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    return f"{path}: unexpected property {extra[0]}"
    if expected_type == "array":
        if not isinstance(value, list):
            return f"{path}: expected array"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                finding = _validate_value(item, item_schema, f"{path}[{index}]")
                if finding is not None:
                    return finding
    return None


def _matches_type(value: IdentityValue, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False
