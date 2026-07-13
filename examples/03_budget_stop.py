from mock_model import call_model

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime

rt = Runtime(MemoryStore())
with rt.run("stop", budget=Budget(steps=0)) as run:
    try:
        run.model_call({"model": "mock-1"}, fn=call_model)
    except BudgetExceeded as exc:
        print(f"stopped by governor refusal={exc.refusal_id[:12]}")
