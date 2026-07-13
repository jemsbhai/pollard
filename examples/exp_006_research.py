"""Record EXP-006A: research and summarize a fixed offline document set."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from _exp006_common import (
    finalize_case,
    local_llama_server,
    sha256_file,
    write_json,
)

from pollard import ActionSpec, Budget, Registry, Runtime, SQLiteStore
from pollard.adapters.openai import make_chat_completions_fn

DOC_IDS = ("DOC-001", "DOC-002", "DOC-003")
UNSUPPORTED_PHRASES = (
    "enhancing performance",
    "maintains availability",
    "reducing redundancy",
)


def _text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise RuntimeError("local model result omitted text")
    return str(result["text"])


def _schemas() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    read = {
        "type": "object",
        "properties": {"document_id": {"type": "string", "enum": list(DOC_IDS)}},
        "required": ["document_id"],
        "additionalProperties": False,
    }
    check = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "consulted_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(DOC_IDS)},
            },
        },
        "required": ["summary", "consulted_ids"],
        "additionalProperties": False,
    }
    return empty, read, check


def _registry(documents: dict[str, dict[str, str]]) -> Registry:
    empty_schema, read_schema, check_schema = _schemas()

    def list_documents(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "documents": [
                {
                    "document_id": doc_id,
                    "title": value["title"],
                    "sha256": value["sha256"],
                }
                for doc_id, value in sorted(documents.items())
            ]
        }

    def read_document(args: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(args["document_id"])
        return {"document_id": doc_id, **documents[doc_id]}

    def check_summary(args: dict[str, Any]) -> dict[str, Any]:
        summary = str(args["summary"])
        normalized = summary.casefold()
        consulted = {str(item) for item in args["consulted_ids"]}
        missing_documents = [doc_id for doc_id in DOC_IDS if doc_id not in consulted]
        missing_citations = [doc_id for doc_id in DOC_IDS if f"[{doc_id}]" not in summary]
        unsupported_phrases = [phrase for phrase in UNSUPPORTED_PHRASES if phrase in normalized]
        return {
            "passed": not missing_documents and not missing_citations and not unsupported_phrases,
            "missing_documents": missing_documents,
            "missing_citations": missing_citations,
            "unsupported_phrases": unsupported_phrases,
        }

    return Registry(
        [
            ActionSpec(
                "list_documents",
                "1",
                "List the pinned offline research documents.",
                empty_schema,
                False,
                list_documents,
            ),
            ActionSpec(
                "read_document",
                "1",
                "Read one pinned offline research document.",
                read_schema,
                False,
                read_document,
            ),
            ActionSpec(
                "check_summary",
                "1",
                "Check source coverage, citation tags, and unsupported benefit phrases.",
                check_schema,
                False,
                check_summary,
            ),
        ]
    )


def _load_documents(path: Path) -> dict[str, dict[str, str]]:
    documents: dict[str, dict[str, str]] = {}
    for doc_id in DOC_IDS:
        doc_path = path / f"{doc_id}.md"
        content = doc_path.read_text(encoding="utf-8")
        title = content.splitlines()[0].removeprefix("# ")
        documents[doc_id] = {
            "title": title,
            "content": content,
            "sha256": sha256_file(doc_path),
        }
    return documents


def run(args: argparse.Namespace) -> dict[str, Any]:
    documents = _load_documents(args.documents)
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "run.db"
    db_path.unlink(missing_ok=True)
    registry = _registry(documents)

    with local_llama_server(args.server_binary, args.model, port=args.port) as client:
        call_model = make_chat_completions_fn(
            client,
            max_tokens=512,
            seed=6006,
            temperature=0,
        )
        with SQLiteStore(db_path) as store:
            runtime = Runtime(store, registry=registry, mode="record")
            with runtime.run("exp-006a-offline-research", budget=Budget(steps=30)) as agent:
                agent.note(
                    {
                        "case": "EXP-006A",
                        "adapter": "openai-compatible-chat",
                        "model": args.model_id,
                        "network": "loopback-only",
                    }
                )
                catalog = agent.tool_call("list_documents", {}, version="1")
                plan_system = "Plan a concise offline research task. Do not invent sources."
                plan_user = (
                    "Plan how to explain Pollard's identity, concurrent budget, and "
                    f"replay guarantees using this catalog:\n{catalog.result}"
                )
                plan = agent.model_call(
                    {
                        "model": args.model_id,
                        "messages": [
                            {
                                "role": "system",
                                "content": plan_system,
                            },
                            {"role": "user", "content": plan_user},
                        ],
                    },
                    fn=call_model,
                )
                branch_parent = agent.cursor_id

                with agent.branch(attempt=0) as narrow:
                    narrow_docs = [
                        narrow.tool_call(
                            "read_document", {"document_id": doc_id}, version="1"
                        ).result
                        for doc_id in DOC_IDS[:2]
                    ]
                    narrow_summary = narrow.model_call(
                        {
                            "model": args.model_id,
                            "messages": [
                                {"role": "system", "content": "Summarize only consulted sources."},
                                {
                                    "role": "user",
                                    "content": (
                                        "Write a short summary with [DOC-NNN] citations. "
                                        f"The unconsulted source must not be cited.\n{narrow_docs}"
                                    ),
                                },
                            ],
                        },
                        fn=call_model,
                    )
                    narrow_text = _text(narrow_summary.result)
                    narrow_check = narrow.tool_call(
                        "check_summary",
                        {"summary": narrow_text, "consulted_ids": list(DOC_IDS[:2])},
                        version="1",
                    )
                    narrow.note({"decision": "reject-incomplete-source-coverage"})
                    narrow.prune()
                    narrow_tip = narrow.cursor_id

                agent.rollback(branch_parent)
                with agent.branch(attempt=1) as complete:
                    complete_docs = [
                        complete.tool_call(
                            "read_document", {"document_id": doc_id}, version="1"
                        ).result
                        for doc_id in DOC_IDS
                    ]
                    final_system = (
                        "Write three short paragraphs from supplied sources. Start them "
                        "[DOC-001], [DOC-002], and [DOC-003] in that order."
                    )
                    final_summary = complete.model_call(
                        {
                            "model": args.model_id,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": final_system,
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "Explain Pollard's identity, concurrent budget, and replay "
                                        f"guarantees in three compact paragraphs.\n{complete_docs}"
                                    ),
                                },
                            ],
                        },
                        fn=call_model,
                    )
                    final_text = _text(final_summary.result)
                    final_check = complete.tool_call(
                        "check_summary",
                        {"summary": final_text, "consulted_ids": list(DOC_IDS)},
                        version="1",
                    )
                    for repair_attempt in range(1, 4):
                        if final_check.result.get("passed"):
                            break
                        repair_system = "Repair tags only; keep the supported meaning."
                        repair_user = "\n".join(
                            [
                                f"Draft: {final_text}",
                                f"Checker: {final_check.result}",
                                "Return exactly three short lines.",
                                "Line 1 starts [DOC-001] and states the identity rule.",
                                "Line 2 starts [DOC-002] and states the budget bound.",
                                "Line 3 starts [DOC-003] and states the replay and seal rule.",
                            ]
                        )
                        repaired = complete.model_call(
                            {
                                "model": args.model_id,
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": repair_system,
                                    },
                                    {"role": "user", "content": repair_user},
                                ],
                            },
                            fn=call_model,
                            attempt=repair_attempt,
                        )
                        final_text = _text(repaired.result)
                        final_check = complete.tool_call(
                            "check_summary",
                            {"summary": final_text, "consulted_ids": list(DOC_IDS)},
                            version="1",
                            attempt=repair_attempt,
                        )
                    if not final_check.result.get("passed"):
                        raise RuntimeError("complete research branch failed citation checks")
                    complete.note({"decision": "select-complete-source-coverage"})
                    selected_tip = complete.cursor_id
                root_id = agent.root_id
                report = agent.report()

    artifact = finalize_case(db_path, root_id, output_dir)
    outcome = {
        "id": "EXP-006A",
        "status": "passed",
        "workload": "research-and-summarize-fixed-offline-documents",
        "adapter": "pollard.adapters.openai.make_chat_completions_fn",
        "model": {
            "id": args.model_id,
            "sha256": sha256_file(args.model),
            "llama_cpp_release": args.llama_release,
            "server_sha256": sha256_file(args.server_binary),
        },
        "documents": {
            doc_id: {"title": value["title"], "sha256": value["sha256"]}
            for doc_id, value in documents.items()
        },
        "registry_digest": registry.registry_digest,
        "plan": _text(plan.result),
        "narrow_check": narrow_check.result,
        "narrow_tip": narrow_tip,
        "selected_check": final_check.result,
        "selected_tip": selected_tip,
        "summary": final_text,
        "report": report,
        "artifact": artifact,
        "provider_spend_usd": 0,
    }
    write_json(output_dir / "outcome.json", outcome)
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", default="qwen2.5-coder:7b")
    parser.add_argument("--llama-release", default="b9630")
    parser.add_argument(
        "--documents",
        type=Path,
        default=Path("evidence/EXP-006/inputs/research"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/EXP-006/research"),
    )
    parser.add_argument("--port", type=int, default=8131)
    args = parser.parse_args()
    outcome = run(args)
    print(outcome["artifact"]["seal_digest"])


if __name__ == "__main__":
    main()
