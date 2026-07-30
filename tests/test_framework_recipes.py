from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pollard import SQLiteStore, verify

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "docs" / "recipes"


def _run_recipe(script: str, database: Path, *arguments: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(RECIPES / script),
            "--database",
            str(database),
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"{script} exited with {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{script} did not emit one JSON document\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        raise AssertionError("unreachable") from exc
    assert isinstance(document, dict)
    return document


def _assert_three_call_recording(database: Path, root_id: str) -> None:
    with SQLiteStore(database, read_only=True) as store:
        assert store.roots() == [root_id]
        nodes = list(store.walk(root_id))
        assert [node.kind for node in nodes] == [
            "root",
            "model_call",
            "tool_call",
            "model_call",
        ]
        assert verify(store, nodes[-1].id).ok


def _assert_two_call_recording(database: Path, root_id: str) -> None:
    with SQLiteStore(database, read_only=True) as store:
        assert store.roots() == [root_id]
        nodes = list(store.walk(root_id))
        assert [node.kind for node in nodes] == [
            "root",
            "tool_call",
            "model_call",
        ]
        assert verify(store, nodes[-1].id).ok


def _assert_three_step_record_replay(
    recorded: dict[str, Any],
    replayed: dict[str, Any],
) -> None:
    assert replayed["root_id"] == recorded["root_id"]
    assert recorded["report"]["avoided"] == {}
    assert recorded["report"]["spent"]["steps"] == 3.0
    assert recorded["report"]["spent"]["tokens"] > 0
    assert replayed["report"]["avoided"]["steps"] == 3.0
    assert (
        replayed["report"]["avoided"]["tokens"]
        == recorded["report"]["spent"]["tokens"]
    )


def _assert_two_step_record_replay(
    recorded: dict[str, Any],
    replayed: dict[str, Any],
) -> None:
    assert replayed["root_id"] == recorded["root_id"]
    assert recorded["report"]["avoided"] == {}
    assert recorded["report"]["spent"]["steps"] == 2.0
    assert recorded["report"]["spent"]["tokens"] > 0
    assert replayed["report"]["avoided"]["steps"] == 2.0
    assert (
        replayed["report"]["avoided"]["tokens"]
        == recorded["report"]["spent"]["tokens"]
    )


def test_langchain_support_rag_rejects_ungrounded_definitive_answers() -> None:
    namespace = runpy.run_path(str(RECIPES / "langchain_support_rag.py"))
    validate = namespace["_validate_citation_ids"]

    with pytest.raises(ValueError, match="must cite a retrieved source"):
        validate("eligible", [], ["returns-electronics"])
    with pytest.raises(ValueError, match="were not retrieved"):
        validate("not_eligible", ["unknown-source"], ["returns-electronics"])
    validate("needs_review", [], ["returns-electronics"])


def test_langchain_incident_response_records_and_strictly_replays(
    tmp_path: Path,
) -> None:
    pytest.importorskip("langchain", minversion="1.3.14")
    pytest.importorskip("pydantic", minversion="2.12")
    database = tmp_path / "langchain-incident.db"

    recorded = _run_recipe(
        "langchain_incident_response.py",
        database,
        "--mode",
        "record",
    )
    replayed = _run_recipe(
        "langchain_incident_response.py",
        database,
        "--mode",
        "replay",
    )

    triage = recorded["triage"]
    assert triage["severity"] == "critical"
    assert triage["requires_paging"] is True
    assert triage["suspected_service"] == "payments"
    assert {"unavailable", "failures", "all customers"} <= set(triage["evidence"])

    runbook = recorded["runbook"]
    assert runbook["service"] == "payments"
    assert runbook["runbook_id"] == "rb-payments-001"
    assert len(runbook["steps"]) == 3

    plan = recorded["plan"]
    assert plan["priority"] == "emergency"
    assert plan["owner"] == "payments-on-call"
    assert plan["runbook_id"] == runbook["runbook_id"]
    assert plan["immediate_actions"] == runbook["steps"]

    assert replayed["triage"] == triage
    assert replayed["runbook"] == runbook
    assert replayed["plan"] == plan
    _assert_three_step_record_replay(recorded, replayed)
    _assert_three_call_recording(database, recorded["root_id"])


def test_langchain_support_rag_records_and_strictly_replays(
    tmp_path: Path,
) -> None:
    pytest.importorskip("langchain", minversion="1.3.14")
    pytest.importorskip("pydantic", minversion="2.12")
    database = tmp_path / "langchain-support-rag.db"

    recorded = _run_recipe(
        "langchain_support_rag.py",
        database,
        "--mode",
        "record",
    )
    replayed = _run_recipe(
        "langchain_support_rag.py",
        database,
        "--mode",
        "replay",
    )

    assert recorded["framework"] == "langchain"
    question = recorded["question"]
    assert "unopened headphones" in question.casefold()
    assert "35 days" in question.casefold()

    retrieved_sources = recorded["retrieved_sources"]
    assert isinstance(retrieved_sources, list)
    assert "returns-electronics" in retrieved_sources

    answer = recorded["answer"]
    assert answer["eligibility"] == "eligible"
    assert "returns-electronics" in answer["citations"]
    assert recorded["ledger_verified"] is True

    assert replayed["framework"] == "langchain"
    assert replayed["question"] == question
    assert replayed["retrieved_sources"] == retrieved_sources
    assert replayed["answer"] == answer
    assert replayed["ledger_verified"] is True
    _assert_two_step_record_replay(recorded, replayed)
    _assert_two_call_recording(database, recorded["root_id"])


def test_pydantic_refund_preview_approval_redaction_and_replay(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pydantic", minversion="2.12")
    database = tmp_path / "pydantic-refund.db"
    sensitive_token = "tok_test_value_must_not_reach_the_ledger"

    document = _run_recipe(
        "pydantic_refund_workflow.py",
        database,
        "--order-id",
        "ord_test_2048",
        "--amount-cents",
        "12500",
        "--customer-token",
        sensitive_token,
    )

    assert document["framework"] == "pydantic"
    assert document["schema_generated_from_model"] is True
    assert document["local_refs_resolved"] is True
    assert document["schema_constraints_registered"] is True
    assert document["preview"] == {
        "dry_run": True,
        "handler_executed": False,
    }
    assert document["approval_required"] is True
    assert document["calls"] == {"handler": 1, "policy": 1}
    assert document["receipt"] == {
        "amount_cents": 12_500,
        "order_id": "ord_test_2048",
        "refund_id": "rfnd_ord_test_2048",
        "status": "submitted",
    }
    assert document["replay"]["same_node"] is True
    assert document["replay"]["same_receipt"] is True
    assert document["replay"]["avoided"]["steps"] == 1.0
    assert document["sensitive_value_stored"] is False
    assert document["ledger_verified"] is True

    database_files = [database, *tmp_path.glob(f"{database.name}-*")]
    assert all(sensitive_token.encode() not in path.read_bytes() for path in database_files)
    with SQLiteStore(database, read_only=True) as store:
        assert document["root_id"] in store.roots()
        assert verify(store, document["root_id"]).ok


def test_pydantic_ai_claim_triage_records_and_strictly_replays(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pydantic_ai", minversion="2.18")
    pytest.importorskip("openai", minversion="2.45")
    database = tmp_path / "pydantic-ai-claim.db"

    recorded = _run_recipe(
        "pydantic_ai_claim_triage.py",
        database,
        "--mode",
        "record",
    )
    replayed = _run_recipe(
        "pydantic_ai_claim_triage.py",
        database,
        "--mode",
        "replay",
    )

    assert recorded["framework"] == "pydantic-ai"
    assert recorded["claim_id"] == "clm_2048"
    decision = recorded["decision"]
    assert decision["decision"] == "manual_review"
    assert decision["risk_level"] == "high"
    assert decision["reserve_cents"] == 185_000
    assert decision["required_evidence"] == [
        "itemized repair invoice",
        "photos of the damaged area",
    ]
    assert "invoice mismatch" in decision["rationale"]

    assert replayed["decision"] == decision
    assert recorded["calls"] == {"model": 2, "tool": 1}
    assert replayed["calls"] == {"model": 0, "tool": 0}
    assert recorded["ledger"] == {
        "model_calls": 2,
        "tool_calls": 1,
        "verified": True,
    }
    assert replayed["ledger"] == recorded["ledger"]
    _assert_three_step_record_replay(recorded, replayed)
    _assert_three_call_recording(database, recorded["root_id"])
