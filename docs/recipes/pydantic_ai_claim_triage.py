"""Govern a typed pydantic-ai claim agent at every model and tool boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from importlib.metadata import version
from typing import Any, Literal, NoReturn


def _strip_volatile(value: Any) -> Any:
    """Remove run-local fields while preserving the semantic request."""

    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in {"conversation_id", "run_id", "timestamp"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _unavailable_handler(_payload: dict[str, Any]) -> NoReturn:
    raise AssertionError("strict replay reached a live claim handler")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from openai import AsyncOpenAI
    from pydantic import BaseModel, ConfigDict, Field
    from pydantic_ai import Agent, RunContext, UsageLimits, models
    from pydantic_ai.capabilities import (
        AbstractCapability,
        WrapModelRequestHandler,
        WrapToolExecuteHandler,
    )
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelResponse,
        ToolCallPart,
    )
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.models.openai import (
        OpenAIResponsesModel,
        OpenAIResponsesModelSettings,
    )
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.tools import ToolDefinition
    from pydantic_core import to_jsonable_python

    from pollard import (
        ActionSpec,
        AsyncRun,
        AsyncRuntime,
        Budget,
        Registry,
        verify,
    )
    from pollard.meters import DepthMeter, StepMeter, TokenMeter, WallClockMeter

    class ClaimLookup(BaseModel):
        model_config = ConfigDict(extra="forbid")

        claim_id: str = Field(description="Claim identifier to retrieve")

    class ClaimRecord(BaseModel):
        model_config = ConfigDict(extra="forbid")

        claim_id: str
        policy_status: Literal["active", "lapsed"]
        amount_cents: int
        deductible_cents: int
        loss_type: Literal["water_damage", "theft", "collision"]
        fraud_signals: list[str]
        documents_received: list[str]

    class ClaimDecision(BaseModel):
        model_config = ConfigDict(extra="forbid")

        decision: Literal["approve", "manual_review", "deny"]
        risk_level: Literal["low", "medium", "high"]
        rationale: str
        required_evidence: list[str]
        reserve_cents: int

    class ModelOnlyTokenMeter(TokenMeter):
        def charge(
            self,
            node_kind: str,
            payload: dict[str, Any],
            result: Any,
            meta: dict[str, Any],
        ) -> int:
            if node_kind == "tool_call":
                return 0
            return super().charge(node_kind, payload, result, meta)

    calls = {"model": 0, "tool": 0}

    def load_claim(raw: dict[str, Any]) -> dict[str, Any]:
        calls["tool"] += 1
        lookup = ClaimLookup.model_validate(raw)
        if lookup.claim_id != args.claim_id:
            raise KeyError(f"unknown demo claim: {lookup.claim_id}")
        return ClaimRecord(
            claim_id=lookup.claim_id,
            policy_status="active",
            amount_cents=185_000,
            deductible_cents=50_000,
            loss_type="water_damage",
            fraud_signals=["invoice total differs from the initial loss estimate"],
            documents_received=["incident report", "repair estimate"],
        ).model_dump(mode="json")

    tool_handler = _unavailable_handler if args.mode == "replay" else load_claim
    registry = Registry(
        [
            ActionSpec(
                name="load_claim",
                version="1",
                description="Load one fixed, read-only insurance claim record.",
                schema=ClaimLookup.model_json_schema(),
                side_effects=False,
                handler=tool_handler,
            )
        ]
    )
    runtime = AsyncRuntime(
        args.database,
        registry=registry,
        meters=[
            StepMeter(),
            DepthMeter(),
            WallClockMeter(),
            ModelOnlyTokenMeter(),
        ],
        mode=args.mode,
    )
    pollard_run = runtime.run(
        "pydantic-ai-claim-triage",
        budget=Budget(tokens=4_000, steps=4),
    )

    class PollardCapability(AbstractCapability[Any]):
        """Map pydantic-ai request hooks onto one sequential Pollard run."""

        def __init__(self, run: AsyncRun) -> None:
            self.run = run

        async def wrap_model_request(
            self,
            ctx: RunContext[Any],
            *,
            request_context: ModelRequestContext,
            handler: WrapModelRequestHandler,
        ) -> ModelResponse:
            del ctx
            messages = ModelMessagesTypeAdapter.dump_python(
                request_context.messages,
                mode="json",
            )
            request_parameters = to_jsonable_python(
                request_context.model_request_parameters
            )
            model_settings = to_jsonable_python(request_context.model_settings)
            payload = {
                "framework": "pydantic-ai",
                "framework_version": version("pydantic-ai"),
                "integration": "pollard-capability-v1",
                "model": request_context.model.model_id,
                "messages": _strip_volatile(messages),
                "model_settings": _strip_volatile(model_settings),
                "request_parameters": _strip_volatile(request_parameters),
            }

            async def invoke_model(_payload: dict[str, Any]) -> dict[str, Any]:
                calls["model"] += 1
                response = await handler(request_context)
                serialized = ModelMessagesTypeAdapter.dump_python(
                    [response],
                    mode="json",
                )
                return {
                    "response": serialized[0],
                    "usage": {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                }

            node = await self.run.amodel_call(payload, fn=invoke_model)
            restored = ModelMessagesTypeAdapter.validate_python(
                [node.result["response"]]
            )
            response = restored[0]
            if not isinstance(response, ModelResponse):
                raise TypeError("recorded pydantic-ai result is not a model response")
            return response

        async def wrap_tool_execute(
            self,
            ctx: RunContext[Any],
            *,
            call: ToolCallPart,
            tool_def: ToolDefinition,
            args: dict[str, Any],
            handler: WrapToolExecuteHandler,
        ) -> Any:
            del ctx, call
            if tool_def.name != "load_claim":
                return await handler(args)
            node = await self.run.atool_call(
                "load_claim",
                args,
                version="1",
            )
            return node.result

    class ClaimTestModel(TestModel):
        def gen_tool_args(self, tool_def: ToolDefinition) -> Any:
            if tool_def.name == "load_claim":
                return {"claim_id": args.claim_id}
            return super().gen_tool_args(tool_def)

    model_settings: dict[str, Any]
    if args.live:
        client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY") or "strict-replay-does-not-connect",
            max_retries=0,
        )
        model = OpenAIResponsesModel(
            args.model,
            provider=OpenAIProvider(openai_client=client),
        )
        model_settings = OpenAIResponsesModelSettings(
            max_tokens=512,
            parallel_tool_calls=False,
            openai_reasoning_effort="none",
            openai_store=False,
        )
    else:
        models.ALLOW_MODEL_REQUESTS = False
        model = ClaimTestModel(
            call_tools=["load_claim"],
            custom_output_args={
                "decision": "manual_review",
                "risk_level": "high",
                "rationale": (
                    "The policy is active, but the invoice mismatch needs a human "
                    "coverage and fraud review."
                ),
                "required_evidence": [
                    "itemized repair invoice",
                    "photos of the damaged area",
                ],
                "reserve_cents": 185_000,
            },
            model_name="claim-triage-v1",
        )
        model_settings = {
            "max_tokens": 512,
            "parallel_tool_calls": False,
        }

    agent = Agent(
        model,
        output_type=ClaimDecision,
        instructions=(
            "You are an insurance claim triage agent. Call load_claim exactly once "
            "before deciding. Never approve a claim with unresolved fraud signals; "
            "return a typed decision with the missing evidence and reserve."
        ),
        model_settings=model_settings,
        retries=0,
        end_strategy="exhaustive",
        max_concurrency=1,
        capabilities=[PollardCapability(pollard_run)],
    )

    @agent.tool_plain(name="load_claim", sequential=True)
    def load_claim_tool(claim_id: str) -> dict[str, Any]:
        """Load the authoritative record for a claim."""

        raise AssertionError(
            f"Pollard capability failed to intercept claim tool for {claim_id}"
        )

    result = await agent.run(
        f"Triage claim {args.claim_id}.",
        usage_limits=UsageLimits(
            request_limit=2,
            tool_calls_limit=1,
            output_tokens_limit=512,
        ),
    )
    decision = ClaimDecision.model_validate(result.output)
    nodes = list(pollard_run.store.walk(pollard_run.root_id))
    return {
        "framework": "pydantic-ai",
        "claim_id": args.claim_id,
        "decision": decision.model_dump(mode="json"),
        "calls": calls,
        "ledger": {
            "model_calls": sum(node.kind == "model_call" for node in nodes),
            "tool_calls": sum(node.kind == "tool_call" for node in nodes),
            "verified": verify(pollard_run.store, pollard_run.cursor_id).ok,
        },
        "root_id": pollard_run.root_id,
        "report": pollard_run.report(),
        "inspect": f"pollard show {args.database} {pollard_run.root_id}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="pydantic-ai-claim.db",
        help="SQLite recording path",
    )
    parser.add_argument(
        "--mode",
        choices=("record", "hybrid", "replay"),
        default="record",
        help="Pollard execution mode (default: record)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use OpenAI instead of pydantic-ai's deterministic TestModel",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("POLLARD_OPENAI_MODEL", "gpt-5.6"),
        help="OpenAI model used with --live",
    )
    parser.add_argument("--claim-id", default="clm_2048")
    args = parser.parse_args()
    if args.live and args.mode != "replay" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set for --live outside replay mode")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    output = asyncio.run(_run(args))
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
