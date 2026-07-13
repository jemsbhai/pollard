"""Show an exact step-budget refusal before the model function executes."""

from mock_model import call_model

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime


def main() -> None:
    runtime = Runtime(MemoryStore())
    with runtime.run("stop", budget=Budget(steps=0)) as run:
        try:
            run.model_call({"model": "mock-1"}, fn=call_model)
        except BudgetExceeded as exc:
            print(f"stopped by governor refusal={exc.refusal_id[:12]}")


if __name__ == "__main__":
    main()
