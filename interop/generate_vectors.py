"""Generate/check portable compatibility fixtures from Pollard's Python core."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

# Keep this read-only reference audit from creating caches outside interop/.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pollard._canon import canonical_bytes  # noqa: E402
from pollard.hashing import node_id, result_digest_from_text, result_text_and_digest  # noqa: E402
from pollard.redaction import redact  # noqa: E402
from pollard.registry import ActionSpec, Registry  # noqa: E402
from pollard.store import MemoryStore  # noqa: E402
from pollard.tree import Node  # noqa: E402
from pollard.verify import verify  # noqa: E402

SAFE_INTEGER_MAX = 9_007_199_254_740_991
FROZEN_ROOT = "56022c0c5fabdad4de7f9226bd28d33cc5c0098d7e1569b1a3b6293d833d8e8e"
FROZEN_MODEL = "07f65f2688e6b6ee1e8acab69b36e29be8d2c72c3317aa47831121a01a7c724d"
FROZEN_RETRY = "e043a7e862a99b31e253c243b0462f259e3d519fa8d6b5b545051f228fba8858"


def canonical_case(name: str, value: object) -> dict[str, object]:
    raw = canonical_bytes(value)  # type: ignore[arg-type]
    return {"name": name, "value": value, "canonical_text": raw.decode("utf-8")}


def node_case(
    name: str,
    kind: str,
    parent: str | None,
    attempt: int,
    payload: dict[str, object],
) -> dict[str, object]:
    identity = {"a": attempt, "k": kind, "p": parent or "", "pl": payload}
    return {
        "name": name,
        "kind": kind,
        "parent": parent,
        "attempt": attempt,
        "payload": payload,
        "canonical_identity_text": canonical_bytes(identity).decode("utf-8"),
        "id": node_id(kind, parent, attempt, payload),
    }


def node_record(node: Node) -> dict[str, object]:
    return {
        "id": node.id,
        "parent": node.parent,
        "kind": node.kind,
        "attempt": node.attempt,
        "payload": node.payload,
        "result": node.result,
        "result_text": node.result_text,
        "result_digest": node.result_digest,
        "meta": node.meta,
    }


def generate() -> dict[str, object]:
    unicode_keys = {"\U00010000": "astral", "\ue000": "private-use", "a": "ascii"}
    integer_keys = {"2": "two", "10": "ten", "02": "zero-two", "0": "zero"}
    controls = {
        "controls": "".join(chr(value) for value in range(32)),
        "special": '\\"/\u2028\u2029',
        "text": "caf\u00e9 \U0001f600",
    }
    canonical = [
        canonical_case("null", None),
        canonical_case("booleans", [False, True]),
        canonical_case("empty_values", {"object": {}, "array": [], "string": ""}),
        canonical_case("safe_integer_boundaries", [-SAFE_INTEGER_MAX, -1, 0, 1, SAFE_INTEGER_MAX]),
        canonical_case("integer_looking_keys", integer_keys),
        canonical_case("unicode_scalar_key_order", unicode_keys),
        canonical_case("escaped_controls_and_unicode", controls),
        canonical_case("nested_key_order", {"z": [{"b": 2, "a": 1}], "a": {"z": None, "a": True}}),
    ]
    nodes = [node_case("golden_root", "root", None, 0, {"run": "golden"})]
    assert nodes[0]["id"] == FROZEN_ROOT, "Python frozen root identity changed"
    golden_payload = {"messages": [{"role": "user", "content": "hello"}], "model": "mock-1"}
    nodes.append(node_case("golden_model", "model_call", FROZEN_ROOT, 0, golden_payload))
    nodes.append(node_case("golden_retry", "model_call", FROZEN_ROOT, 1, golden_payload))
    assert nodes[1]["id"] == FROZEN_MODEL, "Python frozen model identity changed"
    assert nodes[2]["id"] == FROZEN_RETRY, "Python frozen retry identity changed"
    nodes.append(node_case("first_run_root", "root", None, 0, {"run": "first-run"}))
    assert nodes[-1]["id"] == "fb4f2a23cc196e53f0fa800a71c025e0a9b7ac5890b83c4d9d1a0214175d9dd5"
    nodes.append(
        node_case(
            "first_run_model",
            "model_call",
            str(nodes[-1]["id"]),
            0,
            {"model": "local-demo", "prompt": "hello"},
        )
    )
    assert nodes[-1]["id"] == "c4882b75addd9867f623049798e2c6cebc3d49daa80bd5a825c102cf0580fd30"
    for name, payload in [
        ("empty_note", {}),
        ("unicode_note", unicode_keys),
        ("integer_key_note", integer_keys),
        ("control_note", controls),
        ("null_bool_note", {"nil": None, "yes": True, "no": False}),
        ("safe_integer_note", {"min": -SAFE_INTEGER_MAX, "max": SAFE_INTEGER_MAX}),
    ]:
        nodes.append(node_case(name, "note", FROZEN_ROOT, 0, payload))
    nodes.append(node_case("branch_anchor", "note", FROZEN_ROOT, 0, {"branch": True}))

    specs = [
        ActionSpec(
            name="echo",
            version="1.0",
            description="Return fixture input.",
            schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            side_effects=False,
        ),
        ActionSpec(
            name="notify",
            version="2.0",
            description="Send a fixture notification.",
            schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "secret": {"type": "string", "sensitive": True},
                    "nested": {
                        "type": "object",
                        "properties": {"token": {"type": "string", "sensitive": True}},
                        "required": ["token"],
                        "additionalProperties": False,
                    },
                },
                "required": ["message", "secret", "nested"],
                "additionalProperties": False,
            },
            side_effects=True,
        ),
    ]
    registry = Registry(specs)
    assert Registry(list(reversed(specs))).registry_digest == registry.registry_digest
    registry_specs = []
    tools = []
    fixture_args = [
        {"text": "caf\u00e9 \U0001f600"},
        {
            "message": "fixture only",
            "secret": "fake-api-key-for-fixtures",
            "nested": {"token": "fake-token-for-fixtures"},
        },
    ]
    for spec, args in zip(specs, fixture_args, strict=True):
        identity = {
            "name": spec.name,
            "version": spec.version,
            "description": spec.description,
            "schema": spec.schema,
            "side_effects": spec.side_effects,
        }
        registry_specs.append(
            {
                "identity": identity,
                "canonical_text": canonical_bytes(identity).decode("utf-8"),
                "spec_digest": spec.spec_digest,
            }
        )
        assert spec.validate_args(args) is None
        audit_args = spec.redact_args(args)
        payload = {
            "tool": spec.name,
            "version": spec.version,
            "args": audit_args,
            "spec_digest": spec.spec_digest,
            "registry_digest": registry.registry_digest,
        }
        tools.append(
            {
                "name": spec.name,
                "args": args,
                "audit_args": audit_args,
                "node": node_case("registered_" + spec.name, "tool_call", FROZEN_ROOT, 0, payload),
            }
        )

    results = []
    for name, result in [
        ("integer_and_integral_float", {"b": 2.0, "a": 1}),
        (
            "unicode_and_usage",
            {"text": "caf\u00e9 \U0001f600", "usage": {"input_tokens": 2, "output_tokens": 4}},
        ),
    ]:
        text, digest = result_text_and_digest(result)
        results.append(
            {"name": name, "result": result, "result_text": text, "result_digest": digest}
        )
    assert results[0]["result_text"] == '{"a":1,"b":2.0}'
    raw_text = '{ "b": 2.0, "a": 1 }'
    results.append(
        {
            "name": "imported_noncanonical_result_text",
            "result": json.loads(raw_text),
            "result_text": raw_text,
            "result_digest": result_digest_from_text(raw_text),
        }
    )

    store = MemoryStore()
    original_payload = {"run": "detached", "nested": {"flag": True}}
    original_meta = {"nested": {"value": "original"}, "charges": {"steps": 1}}
    root = Node.make(kind="root", parent=None, payload=original_payload, meta=original_meta)
    store.put(root)
    root_before = node_record(store.get(root.id))
    original_payload["nested"]["flag"] = False
    original_meta["nested"]["value"] = "mutated input"
    returned = store.get(root.id)
    returned.payload["nested"]["flag"] = False
    returned.meta["nested"]["value"] = "mutated returned copy"
    assert node_record(store.get(root.id)) == root_before
    child = Node.make(
        kind="model_call",
        parent=root.id,
        payload={"model": "fixture"},
        result={"b": 2.0, "a": 1},
        meta={"charges": {"steps": 1}},
    )
    store.put(child)
    assert verify(store, child.id).ok
    patch = {"nested": {"replacement": True}}
    store.update_meta(root.id, patch)
    patch["nested"]["replacement"] = False
    root_after = node_record(store.get(root.id))
    assert root_after["meta"]["nested"] == {"replacement": True}

    # Bypass normal write validation to exercise Python's imported-record audit.
    tamper_cases = []
    for field, replacement, expected_findings in [
        ("id", "0" * 64, ["node id does not match identity fields"]),
        ("result_text", '{"a":999}', ["result digest does not match stored result"]),
        ("parent", "0" * 64, ["node id does not match identity fields", "node is missing"]),
    ]:
        record = node_record(child)
        record[field] = replacement
        tampered = Node.from_storage(
            id=record["id"],
            parent=record["parent"],
            kind=record["kind"],
            attempt=record["attempt"],
            payload_text=canonical_bytes(record["payload"]).decode("utf-8"),
            result_text=record["result_text"],
            result_digest=record["result_digest"],
            meta_text=json.dumps(record["meta"], allow_nan=False),
        )
        audit_store = MemoryStore()
        audit_store.put(store.get(root.id))
        audit_store._nodes[child.id] = tampered
        report = verify(audit_store, child.id)
        findings = [finding.message for finding in report.findings]
        assert not report.ok and findings == expected_findings
        tamper_cases.append({"field": field, "replacement": replacement, "findings": findings})

    for invalid in [{"x": 0.5}, "\ud800"]:
        try:
            canonical_bytes(invalid)
        except (TypeError, UnicodeEncodeError):
            pass
        else:
            raise AssertionError(f"Python accepted a rejected identity fixture: {invalid!r}")
    for invalid_attempt in [-1, True, 0.5]:
        try:
            Node.make(kind="root", parent=None, payload={}, attempt=invalid_attempt)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"Python accepted an invalid attempt: {invalid_attempt!r}")

    return {
        "format": "pollard-portable-interop-v1",
        "contract": {
            "node_domain": "pollard/v1\n",
            "result_domain": "pollard/v1:result\n",
            "redaction_domain": "pollard/v1:redact\n",
            "safe_integer_min": -SAFE_INTEGER_MAX,
            "safe_integer_max": SAFE_INTEGER_MAX,
            "note": (
                "Generated from local Python source; integer bounds are the native-port subset, "
                "not Python bounds."
            ),
        },
        "canonical_cases": canonical,
        "node_cases": nodes,
        "registry": {
            "specs": registry_specs,
            "registry_digest": registry.registry_digest,
            "canonical_digest_input": canonical_bytes(
                {"spec_digests": sorted(spec.spec_digest for spec in specs)}
            ).decode("utf-8"),
            "registered_tools": tools,
        },
        "redaction_cases": [
            {
                "value": "fake-api-key-for-fixtures",
                "hint": None,
                "marker": redact("fake-api-key-for-fixtures"),
            },
            {
                "value": {"private": True},
                "hint": "fixture",
                "marker": redact({"private": True}, hint="fixture"),
            },
        ],
        "result_cases": results,
        "detached_store": {
            "root_before": root_before,
            "root_after_mutations": root_before,
            "root_after_shallow_patch": root_after,
            "child": node_record(child),
            "verification_ok": True,
            "tamper_cases": tamper_cases,
        },
        "rejected_cases": [
            {
                "name": "fractional_identity",
                "operation": "canonical_bytes",
                "value": {"x": 0.5},
                "error": "invalid_identity",
            },
            {
                "name": "positive_unsafe_integer",
                "operation": "canonical_bytes",
                "integer_text": "9007199254740992",
                "error": "unsafe_integer",
            },
            {
                "name": "negative_unsafe_integer",
                "operation": "canonical_bytes",
                "integer_text": "-9007199254740992",
                "error": "unsafe_integer",
            },
            {
                "name": "negative_attempt",
                "operation": "node_make",
                "attempt": -1,
                "error": "invalid_attempt",
            },
            {
                "name": "boolean_attempt",
                "operation": "node_make",
                "attempt": True,
                "error": "invalid_attempt",
            },
            {
                "name": "fractional_attempt",
                "operation": "node_make",
                "attempt": 0.5,
                "error": "invalid_attempt",
            },
            {
                "name": "unpaired_surrogate",
                "operation": "canonical_bytes",
                "json_text": '"\\ud800"',
                "error": "invalid_unicode",
            },
            {
                "name": "nonroot_missing_parent",
                "operation": "node_make",
                "kind": "note",
                "parent": None,
                "error": "invalid_parent",
            },
            {
                "name": "root_has_parent",
                "operation": "node_make",
                "kind": "root",
                "parent": FROZEN_ROOT,
                "error": "invalid_parent",
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Check fixtures without writing files."
    )
    parser.add_argument(
        "--check-packages",
        action="store_true",
        help="Check fixtures and require both package-local copies to match.",
    )
    args = parser.parse_args()
    expected = (
        json.dumps(generate(), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    )
    path = Path(__file__).with_name("vectors.json")
    if args.check or args.check_packages:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing != expected:
            print(
                "".join(
                    difflib.unified_diff(
                        existing.splitlines(keepends=True),
                        expected.splitlines(keepends=True),
                        fromfile=str(path),
                        tofile="generated from Python",
                    )
                ),
                end="",
            )
            return 1
        snapshots = [
            ROOT / "packages/npm/test/vectors.json",
            ROOT / "crates/pollardai/tests/vectors.json",
        ]
        for snapshot in snapshots:
            if not snapshot.exists():
                if args.check_packages:
                    print(f"Missing package-local fixture: {snapshot.relative_to(ROOT)}")
                    return 1
                continue
            if snapshot.read_text(encoding="utf-8") != expected:
                print(f"Package-local fixture differs: {snapshot.relative_to(ROOT)}")
                return 1
        print("Interop fixtures match local Python sources and frozen identity vectors.")
    else:
        path.write_text(expected, encoding="utf-8", newline="\n")
        print(f"Generated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
