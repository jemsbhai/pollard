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


def test_schema_validator_accepts_supported_values() -> None:
    spec = make_spec()
    assert spec.validate_args({"text": "hello", "limit": 3}) is None


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
