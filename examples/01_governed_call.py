from mock_model import call_model

from pollard import Budget, MemoryStore, Runtime

rt = Runtime(MemoryStore())
with rt.run("triage", budget=Budget(tokens=120_000, depth=8)) as run:
    node = run.model_call(
        {"model": "mock-1", "messages": [{"role": "user", "content": "Summarize: ..."}]},
        fn=call_model,
    )
    print(node.result["text"])
    print(run.report())
