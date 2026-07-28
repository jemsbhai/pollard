"""Run a typed refund through preview, approval, execution, and strict replay."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Literal

from pollard import (
    ActionSpec,
    Budget,
    ConfirmationRequired,
    Decision,
    PolicyContext,
    Registry,
    Runtime,
    SQLiteStore,
    verify,
)
from pollard.meters import DepthMeter, StepMeter, WallClockMeter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="pydantic-refund.db",
        help="SQLite recording path",
    )
    parser.add_argument("--order-id", default="ord_1042")
    parser.add_argument("--amount-cents", type=int, default=12_500)
    parser.add_argument(
        "--customer-token",
        default="tok_demo_customer_1042",
        help="Sensitive demo token; its value is never written to the ledger",
    )
    args = parser.parse_args()

    from pydantic import BaseModel, ConfigDict, Field

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    class CustomerAuthorization(BaseModel):
        model_config = ConfigDict(extra="forbid")

        customer_id: str = Field(description="Customer that owns the order")
        approval_token: str = Field(
            description="Secret token authorizing the refund",
            json_schema_extra={"sensitive": True},
        )

    class RefundRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        order_id: str = Field(description="Order to refund")
        amount_cents: int = Field(description="Refund amount in integer cents")
        reason: Literal["duplicate", "service_failure", "fraud"]
        authorization: CustomerAuthorization

    class RefundReceipt(BaseModel):
        model_config = ConfigDict(extra="forbid")

        refund_id: str
        order_id: str
        amount_cents: int
        status: Literal["submitted"]

    calls = {"handler": 0, "policy": 0}

    def issue_refund(raw: dict[str, Any]) -> dict[str, Any]:
        calls["handler"] += 1
        request = RefundRequest.model_validate(raw)
        return RefundReceipt(
            refund_id=f"rfnd_{request.order_id}",
            order_id=request.order_id,
            amount_cents=request.amount_cents,
            status="submitted",
        ).model_dump(mode="json")

    class LargeRefundApproval:
        def decide(self, ctx: PolicyContext) -> Decision:
            calls["policy"] += 1
            amount = ctx.args["amount_cents"]
            if isinstance(amount, int) and amount >= 10_000:
                return Decision.CONFIRM
            return Decision.ALLOW

    schema = RefundRequest.model_json_schema()

    def make_registry(handler: Any) -> Registry:
        return Registry(
            [
                ActionSpec(
                    name="issue_refund",
                    version="1",
                    description="Submit a customer refund to the payment system.",
                    schema=schema,
                    side_effects=True,
                    handler=handler,
                )
            ]
        )

    request = RefundRequest(
        order_id=args.order_id,
        amount_cents=args.amount_cents,
        reason="service_failure",
        authorization=CustomerAuthorization(
            customer_id="cus_demo_1042",
            approval_token=args.customer_token,
        ),
    )
    request_data = request.model_dump(mode="json")
    registry = make_registry(issue_refund)

    with SQLiteStore(args.database) as store:
        meters = [StepMeter(), DepthMeter(), WallClockMeter()]
        preview_run = Runtime(
            store,
            registry=registry,
            dry_run=True,
            meters=meters,
        ).run(
            "pydantic-refund-preview",
            budget=Budget(steps=2),
        )
        preview = preview_run.tool_call("issue_refund", request_data, version="1")

        execution_run = Runtime(
            store,
            registry=registry,
            policies=[LargeRefundApproval()],
            meters=[StepMeter(), DepthMeter(), WallClockMeter()],
            mode="hybrid",
        ).run("pydantic-refund", budget=Budget(steps=2))
        approval_required = False
        try:
            executed = execution_run.tool_call(
                "issue_refund",
                request_data,
                version="1",
            )
        except ConfirmationRequired as exc:
            approval_required = True
            executed = execution_run.confirm(exc.resume_token)
        receipt = RefundReceipt.model_validate(executed.result)
        audit_text = json.dumps(
            [
                {"payload": node.payload, "result": node.result}
                for root_id in store.roots()
                for node in store.walk(root_id)
            ],
            sort_keys=True,
        )
        resolved_schema = registry.get("issue_refund").schema
        resolved_schema_text = json.dumps(resolved_schema, sort_keys=True)
        execution_root_id = execution_run.root_id
        executed_id = executed.id

    def unreachable_handler(_raw: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("strict replay called the refund handler")

    with SQLiteStore(args.database, read_only=True) as replay_store:
        replay_run = Runtime(
            replay_store,
            registry=make_registry(unreachable_handler),
            meters=[StepMeter(), DepthMeter(), WallClockMeter()],
            mode="replay",
        ).run("pydantic-refund", budget=Budget(steps=2))
        replayed = replay_run.tool_call("issue_refund", request_data, version="1")
        replay_receipt = RefundReceipt.model_validate(replayed.result)
        output = {
            "framework": "pydantic",
            "schema_generated_from_model": True,
            "local_refs_resolved": (
                "$defs" not in resolved_schema_text
                and "$ref" not in resolved_schema_text
            ),
            "preview": {
                "handler_executed": preview.result is not None,
                "dry_run": preview.meta.get("dry_run") is True,
            },
            "approval_required": approval_required,
            "receipt": receipt.model_dump(mode="json"),
            "replay": {
                "same_node": replayed.id == executed_id,
                "same_receipt": replay_receipt == receipt,
                "avoided": replay_run.report()["avoided"],
            },
            "calls": calls,
            "sensitive_value_stored": args.customer_token in audit_text,
            "ledger_verified": verify(replay_store, replayed.id).ok,
            "root_id": execution_root_id,
            "inspect": f"pollard show {args.database} {execution_root_id}",
        }

    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
