"""Record one deterministic model-shaped call and print its budget report."""

from mock_model import call_model

from pollard import Budget, MemoryStore, Runtime


def main() -> None:
    runtime = Runtime(MemoryStore())
    with runtime.run("triage", budget=Budget(tokens=120_000, depth=8)) as run:
        node = run.model_call(
            {
                "model": "mock-1",
                "messages": [{"role": "user", "content": "Summarize: ..."}],
            },
            fn=call_model,
        )
        print(node.result["text"])
        print(run.report())


if __name__ == "__main__":
    main()
