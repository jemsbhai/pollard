"""Answer a return-policy question with grounded, governed LangChain RAG.

The default retriever and model are deterministic and offline. Add ``--live``
to use ChatOpenAI for generation while keeping retrieval local and read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Literal, NoReturn

DEFAULT_QUESTION = "Can I return unopened headphones that were delivered 35 days ago?"
ELECTRONICS_SOURCE_ID = "returns-electronics"


def _unavailable_handler(_payload: dict[str, Any]) -> NoReturn:
    raise AssertionError("strict replay reached a live support handler")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


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


def _parse_support_prompt(prompt: str) -> tuple[str, list[dict[str, Any]]]:
    question_block, separator, passages_text = prompt.rpartition(
        "\n\nRetrieved policy passages (JSON):\n"
    )
    if not separator:
        raise ValueError("support prompt is missing retrieved policy passages")
    question = question_block.removeprefix("Question:\n").strip()
    passages = json.loads(passages_text)
    if not isinstance(passages, list) or not all(
        isinstance(passage, dict) for passage in passages
    ):
        raise TypeError("retrieved policy passages must be a list of objects")
    return question, passages


def _deterministic_answer(
    question: str,
    passages: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized = question.casefold()
    source_ids = {
        source_id
        for passage in passages
        if isinstance((source_id := passage.get("source_id")), str)
    }
    has_electronics_policy = ELECTRONICS_SOURCE_ID in source_ids
    days_match = re.search(r"\b(\d+)\s+(?:calendar\s+)?days?\b", normalized)
    age_days = None if days_match is None else int(days_match.group(1))
    is_unopened = "unopened" in normalized or "factory sealed" in normalized
    is_electronics = "headphone" in normalized or "electronic" in normalized

    if not has_electronics_policy:
        return {
            "eligibility": "needs_review",
            "answer": "The retrieved passages do not establish the applicable return window.",
            "citations": [],
            "next_action": "Ask a support specialist to locate the product-specific policy.",
        }
    if age_days is None or not is_electronics or not is_unopened:
        return {
            "eligibility": "needs_review",
            "answer": (
                "The electronics policy applies, but the question does not confirm "
                "all facts needed to determine eligibility."
            ),
            "citations": [ELECTRONICS_SOURCE_ID],
            "next_action": (
                "Confirm the delivery date, that the headphones are unopened in "
                "their original packaging, and that proof of purchase is available."
            ),
        }
    if age_days <= 45:
        return {
            "eligibility": "eligible",
            "answer": (
                f"Yes. Unopened headphones delivered {age_days} days ago are within "
                "the 45-calendar-day electronics return window, provided they remain "
                "in the original packaging and you have proof of purchase."
            ),
            "citations": [ELECTRONICS_SOURCE_ID],
            "next_action": "Start the return and keep the headphones unopened for inspection.",
        }
    return {
        "eligibility": "not_eligible",
        "answer": (
            f"No under the standard policy. A delivery {age_days} days ago is outside "
            "the 45-calendar-day unopened-electronics return window."
        ),
        "citations": [ELECTRONICS_SOURCE_ID],
        "next_action": "Ask support whether a documented exception applies.",
    }


def _offline_model(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TypeError("model payload must contain prompt messages")
    last = messages[-1]
    if not isinstance(last, dict) or not isinstance(last.get("content"), str):
        raise TypeError("the final prompt message must contain text")
    question, passages = _parse_support_prompt(last["content"])
    text = json.dumps(_deterministic_answer(question, passages), sort_keys=True)
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


def _validate_citation_ids(
    eligibility: str,
    citations: list[str],
    retrieved_source_ids: list[str],
) -> None:
    if eligibility in {"eligible", "not_eligible"} and not citations:
        raise ValueError("a definitive eligibility decision must cite a retrieved source")
    unknown = sorted(set(citations) - set(retrieved_source_ids))
    if unknown:
        raise ValueError(
            "answer cites sources that were not retrieved: " + ", ".join(unknown)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="langchain-support-rag.db",
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
        "--question",
        default=DEFAULT_QUESTION,
        help="Customer return-policy question",
    )
    args = parser.parse_args()
    if args.live and args.mode != "replay" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set for --live outside replay mode")

    # Optional framework imports stay below argument parsing so --help needs no extras.
    from langchain_core.documents import Document
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.runnables import RunnableLambda
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

    class PolicySearchInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        query: str = Field(description="Customer question used to rank policy passages")

    class PolicyPassage(BaseModel):
        model_config = ConfigDict(extra="forbid")

        source_id: str
        text: str

    class PolicySearchResult(BaseModel):
        model_config = ConfigDict(extra="forbid")

        query: str
        matches: list[PolicyPassage]

    class SupportAnswer(BaseModel):
        model_config = ConfigDict(extra="forbid")

        eligibility: Literal["eligible", "not_eligible", "needs_review"]
        answer: str
        citations: list[str]
        next_action: str

    class LocalPolicyRetriever(BaseRetriever):
        """A deterministic lexical retriever over a tiny in-process corpus."""

        documents: list[Any]
        limit: int = 2

        def _get_relevant_documents(
            self,
            query: str,
            *,
            run_manager: Any,
        ) -> list[Any]:
            del run_manager
            query_terms = _tokens(query)

            def rank_key(document: Any) -> tuple[int, str]:
                searchable = f"{document.page_content} {document.metadata.get('title', '')}"
                overlap = len(query_terms & _tokens(searchable))
                source_id = str(document.metadata["source_id"])
                return (-overlap, source_id)

            return sorted(self.documents, key=rank_key)[: self.limit]

    corpus = [
        Document(
            page_content=(
                "Unopened electronics, including headphones, may be returned within "
                "45 calendar days of delivery when the customer has proof of purchase "
                "and the item remains in its original packaging."
            ),
            metadata={
                "source_id": ELECTRONICS_SOURCE_ID,
                "title": "Unopened electronics returns",
            },
        ),
        Document(
            page_content=(
                "Opened headphones may be returned within 14 calendar days of delivery "
                "only when defective. Non-defective opened headphones are not eligible."
            ),
            metadata={
                "source_id": "returns-opened-headphones",
                "title": "Opened headphone exceptions",
            },
        ),
        Document(
            page_content=(
                "Approved returns are refunded to the original payment method after "
                "warehouse inspection. Bank processing can take five to seven days."
            ),
            metadata={
                "source_id": "refund-processing",
                "title": "Refund timing",
            },
        ),
    ]
    retriever = LocalPolicyRetriever(documents=corpus, limit=2)

    def search_policy(raw: dict[str, Any]) -> dict[str, Any]:
        request = PolicySearchInput.model_validate(raw)
        documents = retriever.invoke(request.query)
        return PolicySearchResult(
            query=request.query,
            matches=[
                PolicyPassage(
                    source_id=str(document.metadata["source_id"]),
                    text=document.page_content,
                )
                for document in documents
            ],
        ).model_dump(mode="json")

    tool_handler = _unavailable_handler if args.mode == "replay" else search_policy
    registry = Registry(
        [
            ActionSpec(
                "search_policy",
                "1",
                "Search revision 1 of the fixed local customer-support policy corpus.",
                PolicySearchInput.model_json_schema(),
                False,
                tool_handler,
            )
        ]
    )

    model_id = args.model if args.live else "offline:support-rag-v1"
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
        "langchain-support-rag",
        budget=Budget(tokens=3_000, steps=3),
    ) as run:
        parser_output = PydanticOutputParser(pydantic_object=SupportAnswer)
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Answer the return-policy question using only the retrieved "
                    "passages. Cite only exact source_id values present in the "
                    "passages. If the evidence or customer facts are insufficient, "
                    "choose needs_review.\n{format_instructions}",
                ),
                (
                    "human",
                    "Question:\n{question}\n\nRetrieved policy passages (JSON):\n"
                    "{passages_json}",
                ),
            ]
        ).partial(format_instructions=parser_output.get_format_instructions())

        retrieved_source_ids: list[str] = []

        def retrieve(state: dict[str, str]) -> dict[str, str]:
            node = run.tool_call(
                "search_policy",
                PolicySearchInput(query=state["question"]).model_dump(mode="json"),
                version="1",
            )
            result = PolicySearchResult.model_validate(node.result)
            retrieved_source_ids[:] = [match.source_id for match in result.matches]
            return {
                "question": state["question"],
                "passages_json": json.dumps(
                    [match.model_dump(mode="json") for match in result.matches],
                    sort_keys=True,
                ),
            }

        def governed_model(prompt_value: Any) -> str:
            node = run.model_call(
                {
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

        def validate_grounding(answer: SupportAnswer) -> SupportAnswer:
            _validate_citation_ids(
                answer.eligibility,
                answer.citations,
                retrieved_source_ids,
            )
            return answer

        # Every runnable executes sequentially on the caller thread. This keeps
        # Pollard's mutable cursor and SQLite connection on one linear path.
        workflow = (
            RunnableLambda(retrieve)
            | prompt
            | RunnableLambda(governed_model)
            | parser_output
            | RunnableLambda(validate_grounding)
        )
        answer = workflow.invoke({"question": args.question})
        document = {
            "framework": "langchain",
            "question": args.question,
            "retrieved_sources": retrieved_source_ids,
            "answer": answer.model_dump(mode="json"),
            "root_id": run.root_id,
            "report": run.report(),
            "ledger_verified": verify(run.store, run.cursor_id).ok,
            "inspect": f"pollard show {args.database} {run.root_id}",
        }
        print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
