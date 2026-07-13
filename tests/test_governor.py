from hypothesis import given
from hypothesis import strategies as st

from pollard import Budget, BudgetExceeded, MemoryStore, Runtime, recompute_charges


def test_budget_refusal_happens_before_execution() -> None:
    run = Runtime(MemoryStore()).run("budget", budget=Budget(steps=0))
    called = False

    def fn(payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"payload": payload}

    try:
        run.model_call({"model": "mock-1"}, fn=fn)
    except BudgetExceeded as exc:
        refusal = run.store.get(exc.refusal_id)
    else:
        raise AssertionError("BudgetExceeded was not raised")

    assert not called
    assert refusal.kind == "refusal"
    assert refusal.payload["reason"] == "budget"
    assert refusal.payload["meter"] == "steps"
    assert refusal.payload["blocked_kind"] == "model_call"


def test_post_settle_exhaustion_blocks_the_next_call() -> None:
    run = Runtime(MemoryStore()).run("tokens", budget=Budget(tokens=5))
    run.model_call(
        {"model": "mock-1"},
        fn=lambda _payload: {"usage": {"input_tokens": 4, "output_tokens": 4}},
    )
    called = False

    def second(payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"payload": payload}

    try:
        run.model_call({"model": "mock-1"}, fn=second)
    except BudgetExceeded as exc:
        refusal = run.store.get(exc.refusal_id)
    else:
        raise AssertionError("BudgetExceeded was not raised")

    assert not called
    assert refusal.payload["meter"] == "tokens"


@given(st.lists(st.tuples(st.integers(0, 50), st.integers(0, 50)), min_size=1, max_size=10))
def test_live_counters_match_recomputed_charges(usages: list[tuple[int, int]]) -> None:
    run = Runtime(MemoryStore()).run("charges", budget=Budget(tokens=10_000, steps=100))
    for index, (input_tokens, output_tokens) in enumerate(usages):
        run.model_call(
            {"model": "mock-1", "messages": [{"role": "user", "content": str(index)}]},
            fn=lambda _payload, i=input_tokens, o=output_tokens: {
                "usage": {"input_tokens": i, "output_tokens": o}
            },
        )
    assert run.report()["spent"] == recompute_charges(run.store, run.root_id)
