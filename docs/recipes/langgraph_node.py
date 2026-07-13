"""A LangGraph node whose provider call is ledgered by Pollard."""

import sys
from typing import TypedDict

from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn


class State(TypedDict):
    prompt: str
    answer: str


def main() -> None:
    from langgraph.graph import END, START, StateGraph
    from openai import OpenAI

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runtime = Runtime("langgraph-node.db", mode="hybrid")
    call_openai = make_responses_fn(OpenAI(max_retries=0))

    with runtime.run("langgraph-node", budget=Budget(tokens=2_000, steps=4)) as run:

        def governed_model_node(state: State) -> dict[str, str]:
            node = run.model_call(
                {
                    "model": "gpt-5.5",
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
        print("inspect:", f"pollard show langgraph-node.db {run.root_id}")


if __name__ == "__main__":
    main()
