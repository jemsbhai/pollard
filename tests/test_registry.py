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


def make_value_spec(value_schema: dict[str, object]) -> ActionSpec:
    return ActionSpec(
        "constrained-value",
        "1",
        "Validate one constrained value.",
        {
            "type": "object",
            "properties": {"value": value_schema},
            "required": ["value"],
            "additionalProperties": False,
        },
        False,
    )  # type: ignore[arg-type]


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
        {"type": "integer", "multipleOf": 2},
        {"type": "string", "pattern": "^[a-z]+$"},
        {"type": "array", "uniqueItems": True},
        {"type": "array", "prefixItems": [{"type": "string"}]},
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


@pytest.mark.parametrize(
    "any_of",
    [
        [],
        "not-a-list",
        [None],
        [{"type": "number"}],
    ],
)
def test_schema_rejects_malformed_any_of(any_of: object) -> None:
    with pytest.raises(UnsupportedSchema, match="anyOf"):
        ActionSpec(
            "bad-union",
            "1",
            "Bad union.",
            {
                "type": "object",
                "properties": {"value": {"anyOf": any_of}},
            },
            False,
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("keyword", "schema_type"),
    [
        ("minimum", "integer"),
        ("maximum", "integer"),
        ("exclusiveMinimum", "integer"),
        ("exclusiveMaximum", "integer"),
        ("minLength", "string"),
        ("maxLength", "string"),
        ("minItems", "array"),
        ("maxItems", "array"),
    ],
)
@pytest.mark.parametrize("value", [True, False, "1", None, 1.5, [], {}])
def test_schema_rejects_non_integer_constraint_values(
    keyword: str,
    schema_type: str,
    value: object,
) -> None:
    with pytest.raises(UnsupportedSchema, match=keyword):
        make_value_spec({"type": schema_type, keyword: value})


@pytest.mark.parametrize(
    ("keyword", "schema_type"),
    [
        ("minLength", "string"),
        ("maxLength", "string"),
        ("minItems", "array"),
        ("maxItems", "array"),
    ],
)
def test_schema_rejects_negative_length_constraints(
    keyword: str,
    schema_type: str,
) -> None:
    with pytest.raises(UnsupportedSchema, match="nonnegative"):
        make_value_spec({"type": schema_type, keyword: -1})


@pytest.mark.parametrize(
    ("keyword", "required_type", "wrong_type"),
    [
        ("minimum", "integer", "string"),
        ("maximum", "integer", "string"),
        ("exclusiveMinimum", "integer", "string"),
        ("exclusiveMaximum", "integer", "string"),
        ("minLength", "string", "array"),
        ("maxLength", "string", "array"),
        ("minItems", "array", "string"),
        ("maxItems", "array", "string"),
    ],
)
def test_constraint_keywords_require_their_declared_type(
    keyword: str,
    required_type: str,
    wrong_type: str,
) -> None:
    for value_schema in (
        {keyword: 0},
        {"type": wrong_type, keyword: 0},
    ):
        with pytest.raises(
            UnsupportedSchema,
            match=rf"{keyword}: requires type '{required_type}'",
        ):
            make_value_spec(value_schema)


def test_schema_validator_accepts_supported_values() -> None:
    spec = make_spec()
    assert spec.validate_args({"text": "hello", "limit": 3}) is None


def test_schema_any_of_registers_and_validates_each_branch() -> None:
    spec = ActionSpec(
        "route",
        "1",
        "Route to one destination shape.",
        {
            "type": "object",
            "properties": {
                "destination": {
                    "anyOf": [
                        {"type": "string", "enum": ["broadcast"]},
                        {
                            "type": "object",
                            "properties": {"user_id": {"type": "integer"}},
                            "required": ["user_id"],
                            "additionalProperties": False,
                        },
                    ]
                }
            },
            "required": ["destination"],
            "additionalProperties": False,
        },
        False,
    )

    assert spec.validate_args({"destination": "broadcast"}) is None
    assert spec.validate_args({"destination": {"user_id": 7}}) is None
    assert "does not match anyOf" in (
        spec.validate_args({"destination": "unicast"}) or ""
    )
    assert "does not match anyOf" in (
        spec.validate_args({"destination": {"user_id": True}}) or ""
    )
    assert "does not match anyOf" in (spec.validate_args({"destination": None}) or "")


def test_inclusive_integer_bounds_accept_endpoints() -> None:
    spec = make_value_spec(
        {
            "type": "integer",
            "minimum": -2,
            "maximum": 2,
        }
    )

    for value in (-2, 0, 2):
        assert spec.validate_args({"value": value}) is None
    assert "value must be at least -2" in (spec.validate_args({"value": -3}) or "")
    assert "value must be at most 2" in (spec.validate_args({"value": 3}) or "")


def test_exclusive_integer_bounds_reject_endpoints() -> None:
    spec = make_value_spec(
        {
            "type": "integer",
            "exclusiveMinimum": -2,
            "exclusiveMaximum": 2,
        }
    )

    for value in (-1, 0, 1):
        assert spec.validate_args({"value": value}) is None
    assert "value must be greater than -2" in (
        spec.validate_args({"value": -2}) or ""
    )
    assert "value must be less than 2" in (spec.validate_args({"value": 2}) or "")


def test_string_length_counts_unicode_code_points() -> None:
    spec = make_value_spec(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 2,
        }
    )

    assert spec.validate_args({"value": "é"}) is None
    assert spec.validate_args({"value": "🙂🙂"}) is None
    assert "length must be at least 1" in (spec.validate_args({"value": ""}) or "")
    assert "length must be at most 2" in (
        spec.validate_args({"value": "e\u0301x"}) or ""
    )


def test_array_cardinality_and_item_schema_both_apply() -> None:
    spec = make_value_spec(
        {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 2,
        }
    )

    assert spec.validate_args({"value": [1]}) is None
    assert spec.validate_args({"value": [1, 2]}) is None
    assert "item count must be at least 1" in (
        spec.validate_args({"value": []}) or ""
    )
    assert "item count must be at most 2" in (
        spec.validate_args({"value": [1, 2, 3]}) or ""
    )
    assert "$.value[0]: expected integer" in (
        spec.validate_args({"value": ["one"]}) or ""
    )


def test_zero_length_constraints_accept_empty_values() -> None:
    string_spec = make_value_spec(
        {
            "type": "string",
            "minLength": 0,
            "maxLength": 0,
        }
    )
    array_spec = make_value_spec(
        {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 0,
            "maxItems": 0,
        }
    )

    assert string_spec.validate_args({"value": ""}) is None
    assert array_spec.validate_args({"value": []}) is None


def test_any_of_constraints_are_branch_local_and_booleans_are_not_integers() -> None:
    spec = make_value_spec(
        {
            "anyOf": [
                {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2,
                },
                {
                    "type": "boolean",
                    "enum": [False],
                },
            ]
        }
    )

    assert spec.validate_args({"value": 1}) is None
    assert spec.validate_args({"value": 2}) is None
    assert spec.validate_args({"value": False}) is None
    assert "does not match anyOf" in (spec.validate_args({"value": 0}) or "")
    assert "does not match anyOf" in (spec.validate_args({"value": True}) or "")


@pytest.mark.parametrize(
    ("value_schema", "values"),
    [
        (
            {
                "type": "integer",
                "minimum": 2,
                "maximum": 1,
            },
            [1, 2],
        ),
        (
            {
                "type": "integer",
                "exclusiveMinimum": 1,
                "exclusiveMaximum": 2,
            },
            [1, 2],
        ),
        (
            {
                "type": "string",
                "minLength": 2,
                "maxLength": 1,
            },
            ["a", "ab"],
        ),
        (
            {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 1,
            },
            [[1], [1, 2]],
        ),
    ],
)
def test_contradictory_constraints_register_but_accept_no_boundary_value(
    value_schema: dict[str, object],
    values: list[object],
) -> None:
    spec = make_value_spec(value_schema)

    for value in values:
        assert spec.validate_args({"value": value}) is not None


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


def test_schema_resolves_local_refs_inside_any_of_branches() -> None:
    spec = ActionSpec(
        "referenced-union",
        "1",
        "Union with a referenced branch.",
        {
            "$defs": {
                "account": {
                    "type": "object",
                    "properties": {"account_id": {"type": "integer"}},
                    "required": ["account_id"],
                    "additionalProperties": False,
                }
            },
            "type": "object",
            "properties": {
                "recipient": {
                    "anyOf": [
                        {"$ref": "#/$defs/account"},
                        {"type": "null"},
                    ]
                }
            },
            "required": ["recipient"],
            "additionalProperties": False,
        },
        False,
    )

    assert "$defs" not in spec.schema
    properties = spec.schema["properties"]
    assert isinstance(properties, dict)
    recipient = properties["recipient"]
    assert isinstance(recipient, dict)
    branches = recipient["anyOf"]
    assert isinstance(branches, list)
    assert branches[0] == {
        "type": "object",
        "properties": {"account_id": {"type": "integer"}},
        "required": ["account_id"],
        "additionalProperties": False,
    }
    assert spec.validate_args({"recipient": {"account_id": 42}}) is None
    assert spec.validate_args({"recipient": None}) is None
    assert "does not match anyOf" in (
        spec.validate_args({"recipient": {"account_id": "42"}}) or ""
    )


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


def test_sensitive_redaction_follows_only_matching_any_of_branch() -> None:
    spec = ActionSpec(
        "authenticate",
        "1",
        "Authenticate with a secret or a public label.",
        {
            "type": "object",
            "properties": {
                "auth": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"enum": ["token"]},
                                "value": {"type": "string", "sensitive": True},
                            },
                            "required": ["kind", "value"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"enum": ["label"]},
                                "value": {"type": "string"},
                            },
                            "required": ["kind", "value"],
                            "additionalProperties": False,
                        },
                    ]
                }
            },
            "required": ["auth"],
            "additionalProperties": False,
        },
        False,
    )
    secret = "must-not-be-stored"

    protected = spec.redact_args({"auth": {"kind": "token", "value": secret}})
    protected_auth = protected["auth"]
    assert isinstance(protected_auth, dict)
    marker = protected_auth["value"]
    assert isinstance(marker, dict)
    assert marker["__pollard_redacted"]
    assert secret not in str(protected)

    public = {"auth": {"kind": "label", "value": "customer-facing"}}
    assert spec.redact_args(public) == public

    malformed = spec.redact_args(
        {"auth": {"kind": "token", "value": secret, "unexpected": True}}
    )
    malformed_auth = malformed["auth"]
    assert isinstance(malformed_auth, dict)
    malformed_marker = malformed_auth["value"]
    assert isinstance(malformed_marker, dict)
    assert malformed_marker["__pollard_redacted"]
    assert secret not in str(malformed)


def test_pydantic_nullable_sensitive_string_schema_registers() -> None:
    pytest.importorskip("pydantic", minversion="2.12")
    from pydantic import BaseModel, ConfigDict, Field

    class NullableSecret(BaseModel):
        model_config = ConfigDict(extra="forbid")

        secret: str | None = Field(
            default=None,
            json_schema_extra={"sensitive": True},
        )

    schema = NullableSecret.model_json_schema()
    spec = ActionSpec(
        "nullable-secret",
        "1",
        "Accept an optional secret.",
        schema,
        False,
    )

    assert spec.validate_args({}) is None
    assert spec.validate_args({"secret": None}) is None
    assert spec.validate_args({"secret": "present"}) is None
    assert "does not match anyOf" in (spec.validate_args({"secret": 7}) or "")
    assert spec.redact_args({"secret": None}) == {"secret": None}
    redacted = spec.redact_args({"secret": "present"})
    marker = redacted["secret"]
    assert isinstance(marker, dict)
    assert marker["__pollard_redacted"]
    assert "present" not in str(redacted)


def test_pydantic_generated_integer_and_length_constraints_register() -> None:
    pytest.importorskip("pydantic", minversion="2.12")
    from pydantic import BaseModel, ConfigDict, Field

    class ConstrainedArguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

        inclusive: int = Field(ge=1, le=10)
        exclusive: int = Field(gt=1, lt=10)
        label: str = Field(min_length=2, max_length=5)
        items: list[int] = Field(min_length=1, max_length=3)
        maybe: int | None = Field(default=None, ge=0, le=100)

    spec = ActionSpec(
        "pydantic-constraints",
        "1",
        "Accept Pydantic constraint output.",
        ConstrainedArguments.model_json_schema(),
        False,
    )
    lower = {
        "inclusive": 1,
        "exclusive": 2,
        "label": "éx",
        "items": [1],
        "maybe": 0,
    }
    upper = {
        "inclusive": 10,
        "exclusive": 9,
        "label": "abcde",
        "items": [1, 2, 3],
        "maybe": 100,
    }

    assert spec.validate_args(lower) is None
    assert spec.validate_args(upper) is None
    assert spec.validate_args({**lower, "maybe": None}) is None
    assert "value must be at least 1" in (
        spec.validate_args({**lower, "inclusive": 0}) or ""
    )
    assert "value must be less than 10" in (
        spec.validate_args({**lower, "exclusive": 10}) or ""
    )
    assert "length must be at most 5" in (
        spec.validate_args({**lower, "label": "abcdef"}) or ""
    )
    assert "item count must be at most 3" in (
        spec.validate_args({**lower, "items": [1, 2, 3, 4]}) or ""
    )
    assert "does not match anyOf" in (
        spec.validate_args({**lower, "maybe": 101}) or ""
    )


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
