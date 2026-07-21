"""Framework-neutral helpers for the JSON Schema subset used by Pollard."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from .errors import UnsupportedSchema


def resolve_local_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref`` values and remove definition containers.

    Only references into the supplied schema document are accepted. Recursive
    references are rejected because Pollard's compact validator operates on a
    finite, expanded schema tree.
    """

    root = schema

    def resolve(value: Any, path: str, active: tuple[str, ...]) -> Any:
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise UnsupportedSchema(f"{path}.$ref: must be a string")
            siblings = set(value) - {
                "$defs",
                "$ref",
                "default",
                "definitions",
                "description",
                "sensitive",
                "title",
            }
            if siblings:
                raise UnsupportedSchema(
                    f"{path}.$ref: unsupported sibling keywords {sorted(siblings)}"
                )
            pointer = _local_pointer(reference, path)
            if pointer in active:
                chain = " -> ".join((*active, pointer))
                raise UnsupportedSchema(f"{path}.$ref: cyclic local reference {chain}")
            target = _pointer_target(root, pointer, path)
            expanded = resolve(target, f"reference {reference}", (*active, pointer))
            if not isinstance(expanded, dict):
                raise UnsupportedSchema(f"{path}.$ref: target must be a schema object")
            combined = dict(expanded)
            for name, sibling in value.items():
                if name not in {"$defs", "$ref", "definitions"}:
                    combined[name] = resolve(sibling, f"{path}.{name}", active)
            return combined
        children: dict[str, Any] = {}
        for name, child in value.items():
            if not isinstance(name, str):
                raise UnsupportedSchema(f"{path}: schema keys must be strings")
            if name in {"$defs", "definitions"}:
                continue
            if name == "properties" and isinstance(child, dict):
                children[name] = {
                    property_name: resolve(
                        property_schema,
                        f"{path}.properties.{property_name}",
                        active,
                    )
                    for property_name, property_schema in child.items()
                }
            elif name == "items":
                children[name] = resolve(child, f"{path}.items", active)
            else:
                children[name] = _copy_json(child)
        return children

    resolved = resolve(root, "$", ())
    if not isinstance(resolved, dict):
        raise UnsupportedSchema("schema must resolve to an object")
    return resolved


def schema_has_local_refs(schema: dict[str, Any]) -> bool:
    """Return whether a schema contains reference or definition keywords."""

    pending: list[Any] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if set(value) & {"$ref", "$defs", "definitions"}:
                return True
            properties = value.get("properties")
            if isinstance(properties, dict):
                pending.extend(properties.values())
            if "items" in value:
                pending.append(value["items"])
    return False


def _copy_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {name: _copy_json(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _local_pointer(reference: str, path: str) -> str:
    if reference == "#":
        return ""
    if not reference.startswith("#/"):
        raise UnsupportedSchema(f"{path}.$ref: only local JSON Pointer references are supported")
    fragment = reference[1:]
    for index, character in enumerate(fragment):
        if character == "%" and (
            index + 2 >= len(fragment)
            or any(item not in "0123456789abcdefABCDEF" for item in fragment[index + 1 : index + 3])
        ):
            raise UnsupportedSchema(f"{path}.$ref: invalid percent escape")
    try:
        return unquote(fragment, errors="strict")
    except UnicodeDecodeError as exc:
        raise UnsupportedSchema(f"{path}.$ref: invalid percent escape") from exc


def _pointer_target(root: dict[str, Any], pointer: str, path: str) -> Any:
    current: Any = root
    if not pointer:
        return current
    for raw_token in pointer.removeprefix("/").split("/"):
        token = _pointer_token(raw_token, path)
        if isinstance(current, dict):
            if token not in current:
                raise UnsupportedSchema(f"{path}.$ref: missing local reference target")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdecimal() or int(token) >= len(current):
                raise UnsupportedSchema(f"{path}.$ref: missing local reference target")
            current = current[int(token)]
        else:
            raise UnsupportedSchema(f"{path}.$ref: missing local reference target")
    return current


def _pointer_token(token: str, path: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise UnsupportedSchema(f"{path}.$ref: invalid JSON Pointer escape")
        result.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(result)
