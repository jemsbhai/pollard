import pytest

from pollard import (
    ActionSpec,
    ConfirmationRequired,
    Decision,
    MemoryStore,
    PolicyContext,
    PolicyViolation,
    Registry,
    Runtime,
)


def make_registry(side_effects: bool = False) -> Registry:
    def handler(args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "text": args["text"], "usage": {"input_tokens": 1, "output_tokens": 1}}

    return Registry(
        [
            ActionSpec(
                "echo",
                "1",
                "Echo text.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                side_effects,
                handler,
            )
        ]
    )


def test_registered_tool_call_resolves_through_registry() -> None:
    registry = make_registry()
    run = Runtime(MemoryStore(), registry=registry).run("firewall")
    assert run.store.get(run.root_id).meta["registry_digest"] == registry.registry_digest

    node = run.tool_call("echo", {"text": "hello"}, version="1")

    spec = registry.get("echo")
    assert node.result["text"] == "hello"
    assert node.payload["spec_digest"] == spec.spec_digest
    assert node.payload["registry_digest"] == registry.registry_digest
    assert node.payload["version"] == "1"


def test_registry_blocks_unknown_tool_and_ignores_supplied_fn() -> None:
    run = Runtime(MemoryStore(), registry=make_registry()).run("unknown")
    called = False

    def bypass(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"bad": True}

    with pytest.raises(PolicyViolation) as exc_info:
        run.tool_call("missing", {"text": "hello"}, fn=bypass)

    assert not called
    refusal = run.store.get(exc_info.value.refusal_id)
    assert refusal.kind == "refusal"
    assert refusal.payload["reason"] == "policy"
    assert "unknown registered action" in str(refusal.payload["detail"])


@pytest.mark.parametrize(
    ("kwargs", "args", "detail"),
    [
        ({"version": "2"}, {"text": "hello"}, "unknown registered action"),
        ({}, {"text": 1}, "schema validation failed"),
        ({}, {"text": "hello", "extra": True}, "schema validation failed"),
    ],
)
def test_registry_refuses_version_or_schema_mismatch(
    kwargs: dict[str, str],
    args: dict[str, object],
    detail: str,
) -> None:
    run = Runtime(MemoryStore(), registry=make_registry()).run("refuse")
    with pytest.raises(PolicyViolation) as exc_info:
        run.tool_call("echo", args, **kwargs)
    refusal = run.store.get(exc_info.value.refusal_id)
    assert detail in str(refusal.payload["detail"])
    assert "blocked_payload_digest" in refusal.payload


def test_dry_run_suppresses_side_effectful_handler() -> None:
    registry = make_registry(side_effects=True)
    called = False

    def handler(args: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": args["text"]}

    original = registry.get("echo")
    registry = Registry(
        [
            ActionSpec(
                original.name,
                original.version,
                original.description,
                original.schema,
                True,
                handler,
            )
        ]
    )
    run = Runtime(MemoryStore(), registry=registry, dry_run=True).run("dry")
    node = run.tool_call("echo", {"text": "hello"})
    assert not called
    assert node.result is None
    assert node.meta["dry_run"] is True
    assert node.meta["charges"]["steps"] == 1


def test_dry_run_executes_side_effect_free_handler() -> None:
    run = Runtime(MemoryStore(), registry=make_registry(), dry_run=True).run("dry-safe")
    node = run.tool_call("echo", {"text": "hello"})
    assert node.result["ok"] is True
    assert "dry_run" not in node.meta


def test_unfenced_tool_call_still_uses_supplied_fn() -> None:
    run = Runtime(MemoryStore()).run("unfenced")
    node = run.tool_call(
        "echo",
        {"text": "hello"},
        fn=lambda payload: {"seen": payload, "usage": {"input_tokens": 0, "output_tokens": 0}},
    )
    assert node.result["seen"] == {"tool": "echo", "args": {"text": "hello"}}


class RecordingPolicy:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.contexts: list[PolicyContext] = []

    def decide(self, ctx: PolicyContext) -> Decision:
        self.contexts.append(ctx)
        return self.decision


def test_policy_deny_stops_ordered_composition() -> None:
    first = RecordingPolicy(Decision.DENY)
    second = RecordingPolicy(Decision.CONFIRM)
    run = Runtime(MemoryStore(), registry=make_registry(), policies=[first, second]).run("deny")

    with pytest.raises(PolicyViolation) as exc_info:
        run.tool_call("echo", {"text": "hello"})

    assert len(first.contexts) == 1
    assert second.contexts == []
    refusal = run.store.get(exc_info.value.refusal_id)
    assert refusal.payload["detail"] == "denied by policy"


def test_policy_confirm_resumes_exactly_once() -> None:
    policy = RecordingPolicy(Decision.CONFIRM)
    run = Runtime(MemoryStore(), registry=make_registry(), policies=[policy]).run("confirm")

    with pytest.raises(ConfirmationRequired) as exc_info:
        run.tool_call("echo", {"text": "hello"})

    token = exc_info.value.resume_token
    assert run.cursor_id == run.root_id
    assert not run.store.exists(token)

    node = run.confirm(token)
    assert node.id == token
    assert node.result["text"] == "hello"
    with pytest.raises(KeyError):
        run.confirm(token)


def test_root_rejects_different_registry_for_same_label() -> None:
    store = MemoryStore()
    first = Runtime(store, registry=make_registry()).run("same-registry")
    assert first.root_id
    other = Registry(
        [
            ActionSpec(
                "other",
                "1",
                "Other action.",
                {"type": "object", "additionalProperties": True},
                False,
                lambda _args: {},
            )
        ]
    )
    with pytest.raises(Exception, match="different registry"):
        Runtime(store, registry=other).run("same-registry")
