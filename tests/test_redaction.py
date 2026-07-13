import asyncio
import hashlib
import json
from contextlib import suppress
from pathlib import Path

import pytest

from pollard import ActionSpec, AsyncRuntime, MemoryStore, Registry, Runtime, SQLiteStore, redact
from pollard._canon import canonical_bytes
from pollard.errors import PolicyViolation, UnsupportedSchema
from pollard.stores.hashrope import HashRopeStore


def _registry(seen: list[str], *, side_effects: bool = False) -> Registry:
    def handler(args: dict[str, object]) -> dict[str, object]:
        seen.append(str(args["token"]))
        return {"accepted": True, "usage": {"input_tokens": 0, "output_tokens": 0}}

    return Registry(
        [
            ActionSpec(
                "send",
                "1",
                "Send a protected value.",
                {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "sensitive": True},
                        "label": {"type": "string"},
                    },
                    "required": ["token", "label"],
                    "additionalProperties": False,
                },
                side_effects,
                handler,
            )
        ]
    )


def test_redact_is_deterministic_and_canonical() -> None:
    first = redact("alpha", hint="api token")
    second = redact("alpha")
    other = redact("beta")

    assert first["__pollard_redacted"] == second["__pollard_redacted"]
    assert first["__pollard_redacted"] != other["__pollard_redacted"]
    assert first["hint"] == "api token"
    assert b"alpha" not in canonical_bytes(first)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "sensitive": "yes"},
        {"type": "integer", "sensitive": True},
        {"type": "object", "sensitive": True},
    ],
)
def test_sensitive_schema_keyword_is_fail_closed(schema: dict[str, object]) -> None:
    with pytest.raises(UnsupportedSchema, match="sensitive"):
        ActionSpec("bad", "1", "Bad.", schema, False)  # type: ignore[arg-type]


def test_sensitive_schema_redacts_nested_objects_and_arrays() -> None:
    spec = ActionSpec(
        "nested",
        "1",
        "Nested secrets.",
        {
            "type": "object",
            "properties": {
                "auth": {
                    "type": "object",
                    "properties": {"token": {"type": "string", "sensitive": True}},
                },
                "tokens": {
                    "type": "array",
                    "items": {"type": "string", "sensitive": True},
                },
            },
        },
        False,
    )
    result = spec.redact_args({"auth": {"token": "one"}, "tokens": ["two", "three"]})
    encoded = canonical_bytes(result)
    assert b"one" not in encoded
    assert b"two" not in encoded
    assert b"three" not in encoded


@pytest.mark.parametrize("backend", ["memory", "sqlite", "hashrope"])
def test_sensitive_tool_value_reaches_handler_but_not_store(
    backend: str,
    tmp_path: Path,
) -> None:
    secret = "plaintext-secret-9f77"
    seen: list[str] = []
    if backend == "memory":
        store = MemoryStore()
    elif backend == "sqlite":
        store = SQLiteStore(tmp_path / "redacted.db")
    else:
        store = HashRopeStore()
    try:
        run = Runtime(store, registry=_registry(seen)).run(f"redact-{backend}")
        node = run.tool_call("send", {"token": secret, "label": "ok"})
        assert seen == [secret]
        assert secret not in canonical_bytes(node.payload).decode("utf-8")
        marker = node.payload["args"]["token"]  # type: ignore[index]
        assert isinstance(marker, dict) and marker["__pollard_redacted"]
        if backend == "memory":
            raw = json.dumps([stored.payload for stored in store.walk(run.root_id)]).encode()
        elif backend == "hashrope":
            raw = store.to_bytes()  # type: ignore[union-attr]
        else:
            store.close()  # type: ignore[union-attr]
            raw = (tmp_path / "redacted.db").read_bytes()
        assert secret.encode() not in raw
    finally:
        if backend == "sqlite":
            with suppress(Exception):
                store.close()  # type: ignore[union-attr]


def test_sensitive_value_is_redacted_on_refusal_and_dry_run() -> None:
    secret = "refusal-secret"
    seen: list[str] = []
    run = Runtime(MemoryStore(), registry=_registry(seen)).run("refusal")
    with pytest.raises(PolicyViolation) as exc_info:
        run.tool_call("send", {"token": secret}, version="wrong")
    refusal = run.store.get(exc_info.value.refusal_id)
    assert secret not in canonical_bytes(refusal.payload).decode()

    dry = Runtime(
        MemoryStore(), registry=_registry(seen, side_effects=True), dry_run=True
    ).run("dry")
    node = dry.tool_call("send", {"token": secret, "label": "ok"})
    assert secret not in canonical_bytes(node.payload).decode()
    assert seen == []


def test_async_sensitive_handler_receives_original_only_at_execution() -> None:
    secret = "async-secret"
    seen: list[str] = []

    async def scenario() -> None:
        run = AsyncRuntime(MemoryStore(), registry=_registry(seen)).run("async-redact")
        node = await run.atool_call("send", {"token": secret, "label": "ok"})
        assert secret not in canonical_bytes(node.payload).decode()

    asyncio.run(scenario())
    assert seen == [secret]


def test_redaction_digest_uses_domain_separation() -> None:
    expected = hashlib.sha256(b'pollard/v1:redact\n"value"').hexdigest()
    assert redact("value")["__pollard_redacted"] == expected
