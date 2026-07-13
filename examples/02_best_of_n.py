from mock_model import call_model, judge

from pollard import Budget, MemoryStore, Runtime

rt = Runtime(MemoryStore())
with rt.run("best-of-n", budget=Budget(tokens=120_000, depth=12, steps=20)) as run:
    payload = {"model": "mock-1", "messages": [{"role": "user", "content": "Draft a title"}]}
    best: tuple[int, str] | None = None
    for attempt in range(4):
        with run.branch(attempt=attempt) as branch:
            candidate = branch.model_call(payload, fn=call_model)
            score = branch.tool_call("judge", {"text": candidate.result["text"]}, fn=judge)
            value = int(score.result["value"])
            if best is None or value > best[0]:
                best = (value, candidate.id)
            else:
                branch.prune()

    assert best is not None
    print(f"best score={best[0]} node={best[1][:12]}")
    print(run.report())
