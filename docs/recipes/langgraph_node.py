"""Run a one-node LangGraph whose provider call is governed by Pollard."""

import argparse
import os
import sys
from typing import TypedDict

from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn


class State(TypedDict):
    prompt: str
    answer: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("POLLARD_OPENAI_MODEL", "gpt-5.6"),
        help="OpenAI model ID; defaults to POLLARD_OPENAI_MODEL or gpt-5.6",
    )
    parser.add_argument(
        "--database", default="langgraph-node.db", help="SQLite recording path"
    )
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set before a live run")

    from langgraph.graph import END, START, StateGraph
    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runtime = Runtime(args.database, mode="hybrid")
    call_openai = make_responses_fn(OpenAI(max_retries=0), store=False)

    with runtime.run("langgraph-node", budget=Budget(tokens=2_000, steps=4)) as run:

        def governed_model_node(state: State) -> dict[str, str]:
            node = run.model_call(
                {
                    "model": args.model,
                    "input": state["prompt"],
                    "max_output_tokens": 128,
                    "reasoning": {"effort": "none"},
                },
                fn=call_openai,
            )
            return {"answer": node.result["text"]}

        builder = StateGraph(State)
        builder.add_node("governed_model", governed_model_node)
        builder.add_edge(START, "governed_model")
        builder.add_edge("governed_model", END)
        graph = builder.compile()
        print(graph.invoke({"prompt": "Explain content addressing.", "answer": ""})["answer"])
        print("inspect:", f"pollard show {args.database} {run.root_id}")


if __name__ == "__main__":
    main()
