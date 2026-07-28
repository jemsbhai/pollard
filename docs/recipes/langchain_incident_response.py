"""Triage an incident through a typed, governed, replayable LangChain pipeline.

The default model and runbook are deterministic and offline. Add ``--live`` to
use ChatOpenAI for the two model calls; the read-only runbook remains local.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Literal, NoReturn

DEFAULT_INCIDENT = (
    "The production checkout API is unavailable and payment failures are "
    "affecting all customers."
)
SEVERITIES = ("low", "medium", "high", "critical")


def _unavailable_handler(_payload: dict[str, Any]) -> NoReturn:
    raise AssertionError("strict replay reached a live handler")


def _semantic_messages(prompt_value: Any) -> list[dict[str, str]]:
    """Serialize prompt meaning without LangChain IDs, metadata, or timestamps."""

    roles = {
        "ai": "assistant",
        "human": "user",
        "system": "system",
        "tool": "tool",
    }
    serialized: list[dict[str, str]] = []
    for message in prompt_value.to_messages():
        content = message.content
        if not isinstance(content, str):
            raise TypeError("this recipe expects text-only prompt messages")
        message_type = str(getattr(message, "type", ""))
        serialized.append({"role": roles.get(message_type, message_type), "content": content})
    return serialized


def _offline_model(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TypeError("model payload must contain prompt messages")
    last = messages[-1]
    if not isinstance(last, dict) or not isinstance(last.get("content"), str):
        raise TypeError("the final prompt message must contain text")
    prompt = last["content"]
    stage = payload.get("stage")

    if stage == "triage":
        incident = prompt.rpartition("Incident report:\n")[2].strip()
        output = _deterministic_triage(incident)
    elif stage == "plan":
        incident, triage, runbook = _parse_plan_prompt(prompt)
        output = _deterministic_plan(incident, triage, runbook)
    else:
        raise ValueError(f"unknown offline model stage: {stage!r}")

    text = json.dumps(output, sort_keys=True)
    prompt_tokens = sum(
        len(str(message.get("content", "")).split())
        for message in messages
        if isinstance(message, dict)
    )
    return {
        "text": text,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": len(text.split()),
        },
    }


def _deterministic_triage(incident: str) -> dict[str, Any]:
    normalized = incident.casefold()
    critical_signals = (
        "all customers",
        "breach",
        "data loss",
        "outage",
        "production down",
        "unavailable",
    )
    high_signals = ("degraded", "errors", "failing", "failures", "latency")
    matched = [signal for signal in (*critical_signals, *high_signals) if signal in normalized]

    if any(signal in normalized for signal in critical_signals):
        severity = "critical"
    elif any(signal in normalized for signal in high_signals):
        severity = "high"
    elif any(signal in normalized for signal in ("warning", "intermittent", "slow")):
        severity = "medium"
    else:
        severity = "low"

    if any(word in normalized for word in ("checkout", "payment")):
        service = "payments"
    elif any(word in normalized for word in ("login", "authentication", "identity")):
        service = "identity"
    elif any(word in normalized for word in ("database", "postgres", "mysql")):
        service = "database"
    elif "api" in normalized:
        service = "api-gateway"
    else:
        service = "core-platform"

    summary = " ".join(incident.split())
    return {
        "severity": severity,
        "summary": summary[:240],
        "requires_paging": severity in {"high", "critical"},
        "suspected_service": service,
        "evidence": matched or ["operator report contains no predefined outage signal"],
    }


def _parse_plan_prompt(prompt: str) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    incident_block, separator, remainder = prompt.rpartition("\n\nTriage JSON:\n")
    if not separator:
        raise ValueError("planning prompt is missing triage JSON")
    triage_text, separator, runbook_text = remainder.partition("\n\nRunbook JSON:\n")
    if not separator:
        raise ValueError("planning prompt is missing runbook JSON")
    incident = incident_block.removeprefix("Incident report:\n").strip()
    triage = json.loads(triage_text)
    runbook = json.loads(runbook_text)
    if not isinstance(triage, dict):
        raise TypeError("triage JSON must be an object")
    if runbook is not None and not isinstance(runbook, dict):
        raise TypeError("runbook JSON must be an object or null")
    return incident, triage, runbook


def _deterministic_plan(
    incident: str,
    triage: dict[str, Any],
    runbook: dict[str, Any] | None,
) -> dict[str, Any]:
    severity = str(triage["severity"])
    service = str(triage["suspected_service"])
    runbook_steps = [] if runbook is None else list(runbook.get("steps", []))
    generic_steps = [
        f"Assign the {service} on-call as incident commander.",
        "Confirm customer impact with health checks and recent deployment telemetry.",
        "Post a status update with scope, owner, and the next update time.",
    ]
    actions = [str(step) for step in runbook_steps[:3]] or generic_steps
    return {
        "objective": f"Restore {service} safely and contain impact from: {incident[:160]}",
        "priority": "emergency" if severity == "critical" else "urgent",
        "owner": f"{service}-on-call",
        "immediate_actions": actions,
        "customer_communication": (
            "Acknowledge the service disruption, describe observed impact, and "
            "commit to an update within 15 minutes."
        ),
        "rollback_trigger": (
            "Rollback the latest change if errors remain elevated after the first "
            "mitigation or if impact expands."
        ),
        "runbook_id": None if runbook is None else str(runbook["runbook_id"]),
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    raise TypeError("ChatOpenAI returned non-text content")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="langchain-incident.db",
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
        help="Use ChatOpenAI instead of the deterministic offline model",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("POLLARD_OPENAI_MODEL", "gpt-5.6"),
        help="ChatOpenAI model used with --live",
    )
    parser.add_argument(
        "--incident",
        default=DEFAULT_INCIDENT,
        help="Incident report to triage",
    )
    args = parser.parse_args()
    if args.live and args.mode != "replay" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set for --live outside replay mode")

    # Optional framework imports stay below argument parsing so --help needs no extras.
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import (
        RunnableBranch,
        RunnableLambda,
    )
    from pydantic import BaseModel, ConfigDict, Field

    from pollard import ActionSpec, Budget, Registry, Runtime, verify
    from pollard.meters import DepthMeter, StepMeter, TokenMeter, WallClockMeter

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

    class IncidentTriage(BaseModel):
        model_config = ConfigDict(extra="forbid")

        severity: Literal["low", "medium", "high", "critical"]
        summary: str = Field(description="Concise description of observed impact")
        requires_paging: bool
        suspected_service: str
        evidence: list[str]

    class RunbookLookup(BaseModel):
        model_config = ConfigDict(extra="forbid")

        service: str
        severity: Literal["low", "medium", "high", "critical"]

    class RunbookExcerpt(BaseModel):
        model_config = ConfigDict(extra="forbid")

        service: str
        runbook_id: str
        steps: list[str]
        escalation: str

    class IncidentPlan(BaseModel):
        model_config = ConfigDict(extra="forbid")

        objective: str
        priority: Literal["urgent", "emergency"]
        owner: str
        immediate_actions: list[str]
        customer_communication: str
        rollback_trigger: str
        runbook_id: str | None

    def load_runbook(payload: dict[str, Any]) -> dict[str, Any]:
        request = RunbookLookup.model_validate(payload)
        service = request.service
        excerpts = {
            "payments": [
                "Freeze payment-related deployments and compare the last known-good release.",
                "Check gateway error rate, authorization latency, and dependency health.",
                "Fail over to the secondary payment route if the primary remains unavailable.",
            ],
            "identity": [
                "Check token issuance, signing-key freshness, and identity-provider health.",
                "Freeze authentication deployments and compare the last known-good release.",
                "Enable the documented identity-provider failover if errors remain elevated.",
            ],
            "database": [
                "Check saturation, replication lag, locks, and recent schema changes.",
                "Stop nonessential batch work and protect remaining database capacity.",
                "Fail over only after confirming replica health and recovery-point objectives.",
            ],
            "api-gateway": [
                "Compare gateway 5xx rates by route, zone, and latest deployment.",
                "Drain unhealthy gateway instances and verify upstream dependency health.",
                "Rollback the latest routing change if errors remain elevated.",
            ],
        }
        steps = excerpts.get(
            service,
            [
                "Check service health, recent deployments, and dependency error rates.",
                "Assign an incident commander and freeze unrelated production changes.",
                "Apply the lowest-risk documented mitigation and verify recovery.",
            ],
        )
        return RunbookExcerpt(
            service=service,
            runbook_id=f"rb-{service}-001",
            steps=steps,
            escalation=(
                f"Page the {service} lead immediately"
                if request.severity == "critical"
                else f"Escalate to the {service} lead after 15 minutes without recovery"
            ),
        ).model_dump(mode="json")

    tool_handler = _unavailable_handler if args.mode == "replay" else load_runbook
    registry = Registry(
        [
            ActionSpec(
                "load_runbook",
                "1",
                "Load a local read-only incident runbook excerpt.",
                RunbookLookup.model_json_schema(),
                False,
                tool_handler,
            )
        ]
    )

    model_id = args.model if args.live else "offline:incident-response-v1"
    model_handler: Any
    if args.mode == "replay":
        model_handler = _unavailable_handler
    elif args.live:
        from langchain_openai import ChatOpenAI

        chat_model = ChatOpenAI(
            model=args.model,
            max_retries=0,
            use_responses_api=True,
            store=False,
            reasoning_effort="none",
            max_completion_tokens=512,
        )

        def call_chat_openai(payload: dict[str, Any]) -> dict[str, Any]:
            messages = payload["messages"]
            response = chat_model.invoke(
                [(message["role"], message["content"]) for message in messages]
            )
            usage = response.usage_metadata or {}
            return {
                "text": _message_text(response.content),
                "usage": {
                    "input_tokens": int(usage.get("input_tokens", 0)),
                    "output_tokens": int(usage.get("output_tokens", 0)),
                },
            }

        model_handler = call_chat_openai
    else:
        model_handler = _offline_model
    model_settings_identity = (
        {
            "max_output_tokens": 512,
            "reasoning_effort": "none",
            "retries": 0,
            "store": False,
            "transport": "responses",
        }
        if args.live
        else {"backend": "deterministic", "version": 1}
    )

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    runtime = Runtime(
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
    with runtime.run(
        "langchain-incident-response",
        budget=Budget(tokens=4_000, steps=4),
    ) as run:
        triage_parser = PydanticOutputParser(pydantic_object=IncidentTriage)
        triage_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are an incident commander. Triage the report using only "
                    "the supplied evidence.\n{format_instructions}",
                ),
                ("human", "Incident report:\n{incident}"),
            ]
        ).partial(format_instructions=triage_parser.get_format_instructions())

        plan_parser = PydanticOutputParser(pydantic_object=IncidentPlan)
        plan_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Create a safe, concise incident-response plan grounded in the "
                    "triage and optional runbook.\n{format_instructions}",
                ),
                (
                    "human",
                    "Incident report:\n{incident}\n\nTriage JSON:\n{triage_json}"
                    "\n\nRunbook JSON:\n{runbook_json}",
                ),
            ]
        ).partial(format_instructions=plan_parser.get_format_instructions())

        def governed_model(stage: str) -> Any:
            def call(prompt_value: Any) -> str:
                node = run.model_call(
                    {
                        "stage": stage,
                        "model": model_id,
                        "messages": _semantic_messages(prompt_value),
                        "settings": model_settings_identity,
                    },
                    fn=model_handler,
                )
                text = node.result.get("text")
                if not isinstance(text, str):
                    raise TypeError("recorded model result is missing text")
                return text

            return call

        triage_chain = (
            triage_prompt
            | RunnableLambda(governed_model("triage"))
            | triage_parser
        )

        def with_runbook(state: dict[str, Any]) -> dict[str, Any]:
            triage = state["triage"]
            lookup = RunbookLookup(
                service=triage.suspected_service,
                severity=triage.severity,
            )
            node = run.tool_call(
                "load_runbook",
                lookup.model_dump(mode="json"),
                version="1",
            )
            return {
                **state,
                "runbook": RunbookExcerpt.model_validate(node.result),
            }

        runbook_branch = RunnableBranch(
            (
                lambda state: state["triage"].requires_paging,
                RunnableLambda(with_runbook),
            ),
            RunnableLambda(lambda state: {**state, "runbook": None}),
        )

        def planning_input(state: dict[str, Any]) -> dict[str, str]:
            triage = state["triage"]
            runbook = state["runbook"]
            return {
                "incident": state["incident"],
                "triage_json": json.dumps(
                    triage.model_dump(mode="json"),
                    sort_keys=True,
                ),
                "runbook_json": json.dumps(
                    None if runbook is None else runbook.model_dump(mode="json"),
                    sort_keys=True,
                ),
            }

        plan_chain = (
            RunnableLambda(planning_input)
            | plan_prompt
            | RunnableLambda(governed_model("plan"))
            | plan_parser
        )
        # RunnablePassthrough.assign evaluates mappings in a thread pool. Keep the
        # mutable Pollard cursor and SQLite connection on one thread by making the
        # two state transitions explicit, sequential LCEL steps.
        workflow = (
            RunnableLambda(
                lambda state: {
                    **state,
                    "triage": triage_chain.invoke(state),
                }
            )
            | runbook_branch
            | RunnableLambda(
                lambda state: {
                    **state,
                    "plan": plan_chain.invoke(state),
                }
            )
        )
        result = workflow.invoke({"incident": args.incident})

        triage = result["triage"]
        runbook = result["runbook"]
        plan = result["plan"]
        document = {
            "framework": "langchain",
            "triage": triage.model_dump(mode="json"),
            "runbook": (
                None if runbook is None else runbook.model_dump(mode="json")
            ),
            "plan": plan.model_dump(mode="json"),
            "ledger_verified": verify(run.store, run.cursor_id).ok,
            "root_id": run.root_id,
            "report": run.report(),
            "inspect": f"pollard show {args.database} {run.root_id}",
        }
        print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
