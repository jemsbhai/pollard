import pytest

from pollard import ActionSpec, Registry, UnsupportedSchema

SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["text"],
    "additionalProperties": False,
}


# Frozen vectors: changing these constants means registry identity changed.
SPEC_DIGEST = "9f722b40461b470b67c499258780c9a3a99c10dd4c04dddd458f6c3eeb619de5"
REGISTRY_DIGEST = "f67d5b9bc51454b600bc2fd0ae2e5144d897888d306b0473e37add1b32f4a66c"


def make_spec() -> ActionSpec:
    return ActionSpec(
        name="summarize",
        version="1",
        description="Summarize text.",
        schema=SCHEMA,
        side_effects=False,
        handler=lambda args: {"text": args["text"]},
    )


def test_action_spec_and_registry_digest_golden_vectors() -> None:
    spec = make_spec()
    registry = Registry([spec])
    assert spec.spec_digest == SPEC_DIGEST
    assert registry.registry_digest == REGISTRY_DIGEST


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Registry([make_spec(), make_spec()])


def test_registry_get_enforces_version() -> None:
    registry = Registry([make_spec()])
    assert registry.get("summarize", "1").name == "summarize"
    with pytest.raises(KeyError):
        registry.get("summarize", "2")
    with pytest.raises(KeyError):
        registry.get("missing")


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number"},
        {"type": ["string", "null"]},
        {"type": "object", "patternProperties": {}},
        {"type": "object", "required": "text"},
        {"type": "object", "additionalProperties": {}},
    ],
)
def test_unsupported_schema_is_rejected_at_registration(schema: dict[str, object]) -> None:
    with pytest.raises(UnsupportedSchema):
        ActionSpec("bad", "1", "Bad schema.", schema, False)  # type: ignore[arg-type]


@pytest.mark.parametrize("enum", ["not-a-list", [], ["duplicate", "duplicate"]])
def test_schema_rejects_invalid_enum_shapes(enum: object) -> None:
    with pytest.raises(UnsupportedSchema, match="enum"):
        ActionSpec(
            "bad-enum",
            "1",
            "Bad enum.",
            {"type": "object", "properties": {"value": {"enum": enum}}},
            False,
        )  # type: ignore[arg-type]


def test_schema_validator_accepts_supported_values() -> None:
    spec = make_spec()
    assert spec.validate_args({"text": "hello", "limit": 3}) is None


def test_additional_properties_alone_closes_an_object_schema() -> None:
    spec = ActionSpec(
        "closed",
        "1",
        "Closed arguments.",
        {"additionalProperties": False},
        False,
    )
    assert spec.validate_args({}) is None
    assert "unexpected property value" in (spec.validate_args({"value": 1}) or "")


def test_enum_uses_json_type_equality() -> None:
    spec = ActionSpec(
        "typed-enum",
        "1",
        "Typed enum.",
        {
            "type": "object",
            "properties": {
                "integer": {"enum": [1]},
                "boolean": {"enum": [True]},
            },
            "additionalProperties": False,
        },
        False,
    )
    assert spec.validate_args({"integer": 1, "boolean": True}) is None
    assert "value not in enum" in (spec.validate_args({"integer": True}) or "")
    assert "value not in enum" in (spec.validate_args({"boolean": 1}) or "")


def test_schema_resolves_local_defs_and_escaped_json_pointers() -> None:
    spec = ActionSpec(
        "referenced",
        "1",
        "Referenced schema.",
        {
            "$defs": {
                "path/name": {
                    "type": "object",
                    "properties": {"value": {"$ref": "#/$defs/til~0de"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "til~de": {"type": "string"},
                "space key": {"type": "boolean"},
            },
            "type": "object",
            "properties": {
                "nested": {"$ref": "#/$defs/path~1name"},
                "enabled": {"$ref": "#/%24defs/space%20key"},
            },
            "required": ["nested", "enabled"],
            "additionalProperties": False,
        },
        False,
    )
    assert "$defs" not in spec.schema
    assert spec.validate_args({"nested": {"value": "ok"}, "enabled": True}) is None
    assert "expected string" in (
        spec.validate_args({"nested": {"value": 1}, "enabled": True}) or ""
    )


def test_schema_resolves_legacy_definitions() -> None:
    spec = ActionSpec(
        "legacy-ref",
        "1",
        "Legacy reference.",
        {
            "definitions": {"label": {"type": "string"}},
            "type": "object",
            "properties": {"label": {"$ref": "#/definitions/label"}},
        },
        False,
    )
    assert "definitions" not in spec.schema
    assert spec.validate_args({"label": "ok"}) is None


def test_reference_like_annotation_values_remain_literal() -> None:
    literal = {"$ref": "not-a-schema-reference"}
    spec = ActionSpec(
        "literal-ref",
        "1",
        "Literal reference annotation.",
        {
            "$defs": {"value": {"type": "string"}},
            "type": "object",
            "properties": {
                "mode": {
                    "enum": [literal],
                    "default": literal,
                }
            },
        },
        False,
    )

    properties = spec.schema["properties"]
    assert isinstance(properties, dict)
    mode = properties["mode"]
    assert isinstance(mode, dict)
    assert mode["enum"] == [literal]
    assert mode["default"] == literal


@pytest.mark.parametrize(
    ("schema", "finding"),
    [
        (
            {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}},
            "missing local reference",
        ),
        (
            {
                "$defs": {
                    "a": {"$ref": "#/$defs/b"},
                    "b": {"$ref": "#/$defs/a"},
                },
                "$ref": "#/$defs/a",
            },
            "cyclic local reference",
        ),
        (
            {"$defs": {"bad~key": {"type": "string"}}, "$ref": "#/$defs/bad~2key"},
            "invalid JSON Pointer escape",
        ),
        (
            {"$defs": {"key": {"type": "string"}}, "$ref": "#/$defs/%ZZ"},
            "invalid percent escape",
        ),
        ({"$ref": "https://example.test/schema"}, "only local JSON Pointer"),
    ],
)
def test_schema_rejects_unresolved_or_recursive_refs(
    schema: dict[str, object],
    finding: str,
) -> None:
    with pytest.raises(UnsupportedSchema, match=finding):
        ActionSpec("bad-ref", "1", "Bad reference.", schema, False)  # type: ignore[arg-type]


def test_schema_accepts_non_validation_annotations() -> None:
    spec = ActionSpec(
        "annotated",
        "1",
        "Annotated schema.",
        {
            "title": "AnnotatedArguments",
            "description": "Arguments generated by a schema producer.",
            "type": "object",
            "properties": {
                "count": {
                    "title": "Count",
                    "description": "Number of repetitions.",
                    "default": 1,
                    "type": "integer",
                }
            },
        },
        False,
    )
    assert spec.validate_args({}) is None
    assert spec.validate_args({"count": 2}) is None


@pytest.mark.parametrize("annotation", ["title", "description"])
def test_schema_rejects_non_string_text_annotations(annotation: str) -> None:
    with pytest.raises(UnsupportedSchema, match=annotation):
        ActionSpec("bad", "1", "Bad schema.", {annotation: 1}, False)


@pytest.mark.parametrize(
    ("args", "finding"),
    [
        ({}, "missing required property text"),
        ({"text": "hello", "extra": True}, "unexpected property extra"),
        ({"text": "hello", "limit": True}, "expected integer"),
        ({"text": 5}, "expected string"),
        ({"text": 0.5}, "floats are not allowed"),
    ],
)
def test_schema_validator_reports_first_finding(
    args: dict[str, object],
    finding: str,
) -> None:
    spec = make_spec()
    assert finding in (spec.validate_args(args) or "")


def test_array_null_boolean_and_enum_subset() -> None:
    spec = ActionSpec(
        "classify",
        "1",
        "Classify labels.",
        {
            "type": "object",
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"}},
                "enabled": {"type": "boolean"},
                "mode": {"enum": ["fast", "slow"]},
                "nothing": {"type": "null"},
            },
            "required": ["labels", "enabled"],
            "additionalProperties": False,
        },
        False,
    )
    assert spec.validate_args(
        {"labels": ["a", "b"], "enabled": True, "mode": "fast", "nothing": None}
    ) is None
    assert "expected string" in (spec.validate_args({"labels": [1], "enabled": True}) or "")
    assert "value not in enum" in (
        spec.validate_args({"labels": [], "enabled": True, "mode": "medium"}) or ""
    )
