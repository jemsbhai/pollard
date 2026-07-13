"""A LangGraph node whose provider call is ledgered by Pollard."""

from typing import TypedDict

from pollard import Budget, Runtime
from pollard.adapters.openai import make_responses_fn


class State(TypedDict):
    prompt: str
    answer: str


def main() -> None:
    from langgraph.graph import END, START, StateGraph
    from openai import OpenAI

    runtime = Runtime("langgraph-node.db", mode="hybrid")
    call_openai = make_responses_fn(OpenAI())

    with runtime.run("langgraph-node", budget=Budget(tokens=20_000, steps=4)) as run:

        def governed_model_node(state: State) -> dict[str, str]:
            node = run.model_call(
                {"model": "gpt-5.5", "input": state["prompt"]},
                fn=call_openai,
            )
            return {"answer": node.result["text"]}

        builder = StateGraph(State)
        builder.add_node("governed_model", governed_model_node)
        builder.add_edge(START, "governed_model")
        builder.add_edge("governed_model", END)
        graph = builder.compile()
        print(graph.invoke({"prompt": "Explain content addressing.", "answer": ""})["answer"])
        print("root:", run.root_id)


if __name__ == "__main__":
    main()
