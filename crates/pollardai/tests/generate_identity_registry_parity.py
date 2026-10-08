"""Regenerate differential fixtures using the immutable Pollard 1.6.0 wheel.

Usage: python tests/generate_identity_registry_parity.py /path/to/pollard-1.6.0-py3-none-any.whl
The wheel is the actual PyPI artifact; this does not import this repository's Python code.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import random
import struct
import sys

WHEEL_SHA256 = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
wheel = Path(sys.argv[1]).resolve()
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == WHEEL_SHA256
sys.path.insert(0, str(wheel))
import pollard
from pollard._canon import canonical_bytes
from pollard.hashing import digest_payload, node_id, result_text_and_digest
from pollard.redaction import contains_redaction, is_redacted, redact
from pollard.registry import ActionSpec, Registry
from pollard.errors import UnsupportedSchema

assert pollard.__version__ == "1.6.0"
assert str(wheel) in pollard.__file__

fixtures = {
    "provenance": {
        "package": "pollard", "version": pollard.__version__,
        "source": "https://pypi.org/project/pollard/1.6.0/",
        "wheel": wheel.name, "wheel_sha256": WHEEL_SHA256,
        "generator": Path(__file__).name,
    },
    "identity": [], "results": [], "schemas": [], "redaction_markers": [],
}

identities = [None, True, False, 0, -1, 2**53, -(2**53), 2**63, 2**64-1,
              2**64, -(2**64), 10**120, -(10**120),
              {"\U00010000": "astral", "\ue000": "bmp", "\u001f": "\b\f\n\r\t\"\\", "é": "e\u0301"},
              {"nested": [2**128, {"x": "\u2028\u2029\x7f/"}], "empty": []}]
for value in identities:
    fixtures["identity"].append({
        "value": value, "canonical_text": canonical_bytes(value).decode(),
        "digest": digest_payload(value), "node_id": node_id("note", None, 2**64-1, value),
        "redacted": redact(value, "fixture"),
    })

rng = random.Random(160)
float_values = [0.0, -0.0, 1.0, -1.0, 1e-4, 1e-5, 1e-6, 1e15, 1e16, 1e20, 1e23,
                1.2345678901234567, 5e-324, 1.7976931348623157e308]
for _ in range(256):
    value = struct.unpack("!d", rng.getrandbits(64).to_bytes(8, "big"))[0]
    if math.isfinite(value):
        float_values.append(value)
for value in [*identities, *float_values]:
    text, digest = result_text_and_digest(value)
    fixtures["results"].append({"value": value, "text": text, "digest": digest})

def case(name, schema, args=()):
    record = {"name": name, "schema": schema}
    try:
        spec = ActionSpec(name, "1", "Release differential fixture.", schema, False)
        record.update(accepted=True, resolved_schema=spec.schema, spec_digest=spec.spec_digest)
        validations = []
        for value in args:
            item = {"args": value, "finding": spec.validate_args(value)}
            try:
                item["redacted"] = spec.redact_args(value)
            except (TypeError, ValueError) as exc:
                item["redaction_error"] = type(exc).__name__
            validations.append(item)
        record["validations"] = validations
    except (TypeError, ValueError, UnsupportedSchema) as exc:
        record.update(accepted=False, error=type(exc).__name__, detail=str(exc))
    fixtures["schemas"].append(record)

def field(name, schema, values):
    case(name, {"type": "object", "properties": {"v": schema}, "required": ["v"], "additionalProperties": False},
         [{"v": value} for value in values] + [{}, {"v": values[0], "extra": 1}])

values = [None, True, False, 0, 1, 1.0, -1, 2**64, -(2**128), "", "🙂", "é", "e\u0301", [], [1], {}, {"x": 1}]
for kind in ["integer", "string", "object", "array", "null", "boolean"]:
    field("type_" + kind, {"type": kind}, values)
for bound in [-(10**80), -1, 0, 1, 10**80]:
    for keyword in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"]:
        field(f"{keyword}_{bound}", {"type": "integer", keyword: bound},
              [bound-1, bound, bound+1, True, None, str(bound)])
for kind, keyword, test_values in [
    ("string", "minLength", ["", "🙂", "e\u0301", "abc"]),
    ("string", "maxLength", ["", "🙂", "e\u0301", "abc"]),
    ("array", "minItems", [[], [1], [1, 2], [1, 2, 3]]),
    ("array", "maxItems", [[], [1], [1, 2], [1, 2, 3]]),
]:
    for bound in [0, 1, 2, 10**80]:
        field(f"{keyword}_{bound}", {"type": kind, keyword: bound}, test_values)
for keyword in ["type", "properties", "required", "enum", "anyOf", "additionalProperties", "title", "description", "sensitive"]:
    case("null_" + keyword, {keyword: None}, [{}, {"x": 1}, 1, None])
case("null_properties_additional", {"properties": None, "additionalProperties": False}, [{"extra": 1}, {}])
case("constraint_priority", {"properties": {"z": {"type": "string"}, "a": {"type": "integer"}}, "additionalProperties": False}, [{"a": False, "z": 0, "extra": 1}])
case("sorted_extra_priority", {"additionalProperties": False}, [{"z": 1, "a": 2}])
case("empty_schema", {}, values)
field("typed_enum", {"enum": [1, False, {"x": 1}, [True]]}, values + [[True], [1], {"x": True}])
field("array_items", {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}, [[], [1], [1, 2], [1, 2, 3], [True], [1, "x"]])
field("anyof_sensitive", {"anyOf": [{"type": "string"}, {"type": "null"}], "sensitive": True}, [None, "secret", 42])
field("branch_sensitive", {"anyOf": [
    {"type": "object", "properties": {"kind": {"enum": ["token"]}, "v": {"type": "string", "sensitive": True}}, "required": ["kind", "v"]},
    {"type": "object", "properties": {"kind": {"enum": ["label"]}, "v": {"type": "string"}}, "required": ["kind", "v"]},
]}, [{"kind": "token", "v": "secret"}, {"kind": "label", "v": "public"}, {"kind": "invalid", "v": "secret"}])
field("array_sensitive", {"type": "array", "items": {"type": "string", "sensitive": True}}, [["one", "two"], [], [None]])

for ref, key in [("#/$defs/path~1name", "path/name"), ("#/$defs/til~0de", "til~de"),
                 ("#/%24defs/space%20key", "space key"), ("#/$defs/%C3%A9", "é"), ("#/$defs/", "")]:
    case("local_" + key, {"$defs": {key: {"type": "string"}}, "type": "object", "properties": {"v": {"$ref": ref, "sensitive": True}}}, [{"v": "secret"}, {"v": 1}])
case("legacy_reference", {"definitions": {"v": {"type": "integer"}}, "$ref": "#/definitions/v", "description": "override"}, [1, True])
case("array_pointer", {"$defs": {"schemas": [{"type": "string"}, {"type": "integer"}]}, "$ref": "#/$defs/schemas/01"}, [1, "x"])
case("unused_invalid_def", {"$defs": {"bad": {"type": "number"}}, "type": "object"}, [{}])
case("literal_annotations", {"$defs": {}, "properties": {"v": {"enum": [{"$ref": "literal"}], "default": {"$ref": "literal"}}}}, [{"v": {"$ref": "literal"}}])
case("nested_refs", {"$defs": {"secret": {"type": "string", "sensitive": True}}, "properties": {"v": {"anyOf": [{"type": "array", "items": {"$ref": "#/$defs/secret"}}, {"type": "null"}]}}}, [{"v": ["secret"]}, {"v": None}])
for index, schema in enumerate([
    {"$ref": "#"}, {"$ref": "https://example.invalid/schema"}, {"$ref": "#/missing"},
    {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
    {"$defs": {"a": {"type": "string"}}, "$ref": "#/$defs/a", "type": "string"},
    {"$defs": {"a": 1}, "$ref": "#/$defs/a"}, {"$ref": None}, {"$ref": 1},
    *[{"$ref": f"#/$defs/{token}"} for token in ["%", "%0", "%ZZ", "%FF", "~", "~2", "-"]],
    {"type": "number"}, {"type": ["string", "null"]}, {"enum": []}, {"enum": [True, True]},
    {"anyOf": []}, {"items": None}, {"type": "string", "pattern": ".*"},
    {"type": "integer", "sensitive": True}, {"sensitive": 1}, {"title": 1},
    {"type": "array", "minItems": -1}, {"type": "integer", "minimum": True},
    {"minimum": 1}, {"required": [1]}, {"properties": []},
]):
    case(f"rejected_{index}", schema)

marker = redact("secret")
for value in [marker, {**marker, "extra": 1}, {**marker, "hint": 1}, {**marker, "hint": "hint"},
              {**marker, "__pollard_redacted": "A" * 64}, {"nested": [marker]}, {}, None]:
    fixtures["redaction_markers"].append({"value": value, "is_redacted": is_redacted(value), "contains_redaction": contains_redaction(value)})
specs = [ActionSpec(name, "1", "", {}, False) for name in ["z", "a", "m"]]
registry = Registry(specs)
fixtures["registry"] = {"order": [spec.name for spec in registry], "digest": registry.registry_digest}
out = Path(__file__).with_name("pypi160_identity_registry.json")
out.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {out}: {len(fixtures['schemas'])} schemas, {len(fixtures['identity'])} identities, {len(fixtures['results'])} results")
