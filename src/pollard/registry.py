"""Versioned action registry and zero-dependency schema validation."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ._canon import IdentityValue, canonical_bytes
from .errors import UnsupportedSchema
from .redaction import redact
from .schema import resolve_local_refs, schema_has_local_refs

ActionResult = dict[str, Any] | Awaitable[dict[str, Any]]
ActionHandler = Callable[[dict[str, Any]], ActionResult]

_SUPPORTED_KEYS = {
    "type",
    "properties",
    "required",
    "enum",
    "anyOf",
    "items",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "additionalProperties",
    "title",
    "description",
    "default",
    "sensitive",
}
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
        if schema_has_local_refs(self.schema):
            object.__setattr__(self, "schema", resolve_local_refs(self.schema))
        _check_schema(self.schema, f"schema for {self.name}")
        object.__setattr__(self, "spec_digest", _digest_spec(self))

    def validate_args(self, args: dict[str, IdentityValue]) -> str | None:
        try:
            canonical_bytes(args)
        except TypeError as exc:
            return str(exc)
        return _validate_value(args, self.schema, "$")

    def redact_args(
        self,
        args: dict[str, IdentityValue],
    ) -> dict[str, IdentityValue]:
        """Return the audit form of args with sensitive string fields redacted."""

        redacted = _redact_sensitive(args, self.schema)
        if not isinstance(redacted, dict):
            raise TypeError("action arguments must redact to an object")
        return redacted


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
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise UnsupportedSchema(f"{path}.enum: must be a non-empty list")
        encoded: set[bytes] = set()
        for item in enum:
            item_bytes = canonical_bytes(item)
            if item_bytes in encoded:
                raise UnsupportedSchema(f"{path}.enum: values must be unique")
            encoded.add(item_bytes)
    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise UnsupportedSchema(
                f"{path}.anyOf: must be a non-empty list of schemas"
            )
        for index, child_schema in enumerate(any_of):
            _check_schema(child_schema, f"{path}.anyOf[{index}]")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    for keyword in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    ):
        if keyword in schema:
            _check_constraint(
                schema[keyword],
                keyword=keyword,
                path=path,
                schema_type=schema_type,
                required_type="integer",
                nonnegative=False,
            )
    for keyword, required_type in (
        ("minLength", "string"),
        ("maxLength", "string"),
        ("minItems", "array"),
        ("maxItems", "array"),
    ):
        if keyword in schema:
            _check_constraint(
                schema[keyword],
                keyword=keyword,
                path=path,
                schema_type=schema_type,
                required_type=required_type,
                nonnegative=True,
            )
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise UnsupportedSchema(f"{path}.additionalProperties: must be a boolean")
    for annotation in ("title", "description"):
        value = schema.get(annotation)
        if value is not None and not isinstance(value, str):
            raise UnsupportedSchema(f"{path}.{annotation}: must be a string")
    sensitive = schema.get("sensitive")
    if sensitive is not None and not isinstance(sensitive, bool):
        raise UnsupportedSchema(f"{path}.sensitive: must be a boolean")
    if sensitive is True and not _is_sensitive_string_schema(schema):
        raise UnsupportedSchema(f"{path}.sensitive: only string fields may be sensitive")
    canonical_bytes(schema)


def _redact_sensitive(
    value: IdentityValue,
    schema: dict[str, IdentityValue],
) -> IdentityValue:
    if schema.get("sensitive") is True and isinstance(value, str):
        return redact(value)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branches = [
            branch_schema
            for branch_schema in any_of
            if isinstance(branch_schema, dict)
        ]
        matching = [
            branch_schema
            for branch_schema in branches
            if _validate_value(value, branch_schema, "$") is None
        ]
        for branch_schema in matching or branches:
            value = _redact_sensitive(value, branch_schema)
    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return value
        result = dict(value)
        for name, property_schema in properties.items():
            if name in result and isinstance(property_schema, dict):
                result[name] = _redact_sensitive(result[name], property_schema)
        return result
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [_redact_sensitive(item, item_schema) for item in value]
    return value


def _validate_value(
    value: IdentityValue,
    schema: dict[str, IdentityValue],
    path: str,
) -> str | None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _matches_type(value, expected_type):
        return f"{path}: expected {expected_type}"
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(value, item) for item in enum):
        return f"{path}: value not in enum"
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not any(
        isinstance(child_schema, dict)
        and _validate_value(value, child_schema, path) is None
        for child_schema in any_of
    ):
        return f"{path}: value does not match anyOf"
    if expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
        finding = _validate_integer_bounds(value, schema, path)
        if finding is not None:
            return finding
    if expected_type == "string" and isinstance(value, str):
        finding = _validate_length(
            value, schema, path, "minLength", "maxLength", "length"
        )
        if finding is not None:
            return finding
    if expected_type == "array" and isinstance(value, list):
        finding = _validate_length(
            value, schema, path, "minItems", "maxItems", "item count"
        )
        if finding is not None:
            return finding
    if expected_type == "object" or (
        expected_type is None
        and (
            "properties" in schema
            or "required" in schema
            or "additionalProperties" in schema
        )
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


def _json_equal(left: IdentityValue, right: IdentityValue) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _check_constraint(
    value: IdentityValue,
    *,
    keyword: str,
    path: str,
    schema_type: IdentityValue,
    required_type: str,
    nonnegative: bool,
) -> None:
    if schema_type != required_type:
        raise UnsupportedSchema(
            f"{path}.{keyword}: requires type {required_type!r}"
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedSchema(f"{path}.{keyword}: must be an integer")
    if nonnegative and value < 0:
        raise UnsupportedSchema(f"{path}.{keyword}: must be nonnegative")


def _validate_integer_bounds(
    value: int,
    schema: dict[str, IdentityValue],
    path: str,
) -> str | None:
    minimum = schema.get("minimum")
    if isinstance(minimum, int) and not isinstance(minimum, bool) and value < minimum:
        return f"{path}: value must be at least {minimum}"
    maximum = schema.get("maximum")
    if isinstance(maximum, int) and not isinstance(maximum, bool) and value > maximum:
        return f"{path}: value must be at most {maximum}"
    exclusive_minimum = schema.get("exclusiveMinimum")
    if (
        isinstance(exclusive_minimum, int)
        and not isinstance(exclusive_minimum, bool)
        and value <= exclusive_minimum
    ):
        return f"{path}: value must be greater than {exclusive_minimum}"
    exclusive_maximum = schema.get("exclusiveMaximum")
    if (
        isinstance(exclusive_maximum, int)
        and not isinstance(exclusive_maximum, bool)
        and value >= exclusive_maximum
    ):
        return f"{path}: value must be less than {exclusive_maximum}"
    return None


def _validate_length(
    value: str | list[IdentityValue],
    schema: dict[str, IdentityValue],
    path: str,
    minimum_keyword: str,
    maximum_keyword: str,
    measure: str,
) -> str | None:
    minimum = schema.get(minimum_keyword)
    if isinstance(minimum, int) and not isinstance(minimum, bool) and len(value) < minimum:
        return f"{path}: {measure} must be at least {minimum}"
    maximum = schema.get(maximum_keyword)
    if isinstance(maximum, int) and not isinstance(maximum, bool) and len(value) > maximum:
        return f"{path}: {measure} must be at most {maximum}"
    return None


def _is_sensitive_string_schema(schema: dict[str, IdentityValue]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "string":
        return True
    any_of = schema.get("anyOf")
    if not isinstance(any_of, list) or not any_of:
        return False
    branch_types: set[str] = set()
    for child_schema in any_of:
        if not isinstance(child_schema, dict):
            return False
        child_type = child_schema.get("type")
        if not isinstance(child_type, str):
            return False
        branch_types.add(child_type)
    return "string" in branch_types and branch_types <= {"string", "null"}
