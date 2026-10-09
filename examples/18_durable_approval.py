"""Persist an exact tool approval across worker reconstruction without a network."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pollard import (
    ActionSpec,
    Budget,
    PolicyViolation,
    Registry,
    Runtime,
    SQLiteStore,
    Team,
)
from pollard.approvals import ApprovalPolicy, ApprovalRequest, decide_approval, request_approval
from pollard.team_reports import team_report

ARGS: dict[str, Any] = {"operation_id": "demo-operation-001", "message": "Report is ready."}


def run_demo(directory: Path) -> dict[str, Any]:
    database = directory / "team.db"
    request_path = directory / "request.json"
    checkpoint_path = directory / "checkpoint.json"
    receipts: dict[str, str] = {}
    handler_calls = 0

    def enqueue(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        # A real external service must persist this key and enforce uniqueness.
        # This in-memory stand-in stays alive while the Pollard worker is rebuilt.
        operation_id = str(args["operation_id"])
        receipt = receipts.setdefault(operation_id, f"receipt-{len(receipts) + 1}")
        return {"receipt": receipt, "usage": {"input_tokens": 0, "output_tokens": 0}}

    registry = Registry(
        [
            ActionSpec(
                "enqueue",
                "1",
                "Record a dummy delivery receipt.",
                {
                    "type": "object",
                    "properties": {
                        "operation_id": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["operation_id", "message"],
                    "additionalProperties": False,
                },
                True,
                enqueue,
            ),
        ]
    )

    def open_team(store: SQLiteStore) -> Team:
        return Team(
            Runtime(store, registry=registry, policies=[ApprovalPolicy(store)]),
            "approved-delivery",
            budget=Budget(steps=1),
            allowed_tools=("enqueue",),
        )

    with SQLiteStore(database) as store:
        team = open_team(store)
        root_id = team.root_id
        agent = team.agent("sender", task_id="deliver-report", role="delivery")
        request = request_approval(agent.run, "enqueue", ARGS)
        try:
            agent.run.tool_call("enqueue", ARGS)
        except PolicyViolation:
            paused = True
        else:
            raise AssertionError("delivery must wait for the recorded approval")
        assert handler_calls == 0
        request_path.write_text(json.dumps(request.to_dict()), encoding="utf-8")
        checkpoint_path.write_text(json.dumps(agent.checkpoint().to_dict()), encoding="utf-8")

    # This is the caller-controlled reviewer boundary. A real service first
    # authenticates the reviewer and checks their authority to approve ARGS.
    with SQLiteStore(database) as store:
        request = ApprovalRequest.from_dict(json.loads(request_path.read_text(encoding="utf-8")))
        decision = decide_approval(store, request, approved=True, reviewer="demo-reviewer")

    # The old Runtime and Run are not reused. No confirmation token grants approval.
    with SQLiteStore(database) as store:
        team = open_team(store)
        restored = team.restore(json.loads(checkpoint_path.read_text(encoding="utf-8")))
        result = restored.run.tool_call("enqueue", ARGS)

    with SQLiteStore(database, read_only=True) as store:
        report = team_report(store, root_id)
    assert handler_calls == len(receipts) == 1
    return {
        "network_used": False,
        "hosted_model_spend_usd": 0,
        "paused_before_approval": paused,
        "worker_reconstructed": True,
        "approval_request_id": request.id,
        "approval_decision_id": decision.id,
        "operation_id": ARGS["operation_id"],
        "handler_calls": handler_calls,
        "side_effect_count": len(receipts),
        "receipt": result.result["receipt"],
        "report": report.to_dict(),
    }


def main() -> None:
    with TemporaryDirectory(prefix="pollard-approval-") as directory:
        result = run_demo(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
