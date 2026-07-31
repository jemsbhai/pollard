import asyncio

import pytest

from pollard import AsyncRuntime, BudgetExceeded, MemoryStore, Runtime, SQLiteStore
from pollard.hashing import digest_payload
from pollard.meters import MeterPrecheckRefusal


class RefusingMeter:
    name = "tokens"

    def charge(
        self,
        node_kind: str,
        payload: dict[str, object],
        result: object,
        meta: dict[str, object],
    ) -> int:
        del node_kind, payload, result, meta
        return 0

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, object],
    ) -> None:
        del node_kind, payload
        raise MeterPrecheckRefusal(
            "model_profile_limit",
            "request input exceeds the model profile limit",
            audit_meta={
                "tokenmaster": {
                    "limits": {
                        "ok": False,
                        "input_tokens": 11,
                        "max_input_tokens": 10,
                    }
                }
            },
            requested=11,
            remaining=10,
        )


class BrokenMeter:
    name = "broken"

    def charge(
        self,
        node_kind: str,
        payload: dict[str, object],
        result: object,
        meta: dict[str, object],
    ) -> int:
        del node_kind, payload, result, meta
        return 0

    def precheck_estimate(
        self,
        node_kind: str,
        payload: dict[str, object],
    ) -> None:
        del node_kind, payload
        raise RuntimeError("meter configuration failed")


def test_meter_precheck_refusal_is_audited_before_sync_dispatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    called = False
    payload = {"model": "test:model", "messages": [{"role": "user", "content": "hi"}]}

    def dispatch(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": "should not run"}

    with SQLiteStore(tmp_path / "meter-refusal.db") as store:
        run = Runtime(store, meters=[RefusingMeter()]).run("meter-refusal")
        with pytest.raises(
            BudgetExceeded,
            match="request input exceeds the model profile limit",
        ) as exc_info:
            run.model_call(payload, fn=dispatch)

        refusal = store.get(exc_info.value.refusal_id)
        assert refusal.kind == "refusal"
        assert refusal.parent == run.root_id
        assert refusal.payload == {
            "reason": "model_profile_limit",
            "meter": "tokens",
            "requested": "11",
            "remaining": "10",
            "detail": "request input exceeds the model profile limit",
            "blocked_kind": "model_call",
            "blocked_payload_digest": digest_payload(payload),
        }
        assert refusal.meta["tokenmaster"] == {
            "limits": {
                "ok": False,
                "input_tokens": 11,
                "max_input_tokens": 10,
            }
        }
        assert isinstance(refusal.meta["created_at"], str)
        assert run.cursor_id == refusal.id
        assert not called


def test_meter_precheck_refusal_is_audited_before_async_dispatch() -> None:
    async def scenario() -> None:
        called = False

        async def dispatch(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal called
            called = True
            return {"text": "should not run"}

        store = MemoryStore()
        run = AsyncRuntime(store, meters=[RefusingMeter()]).run("async-meter-refusal")
        with pytest.raises(BudgetExceeded) as exc_info:
            await run.amodel_call({"model": "test:model"}, fn=dispatch)

        refusal = store.get(exc_info.value.refusal_id)
        assert refusal.payload["reason"] == "model_profile_limit"
        assert refusal.meta["tokenmaster"]["limits"]["ok"] is False
        assert run.cursor_id == refusal.id
        assert not called

    asyncio.run(scenario())


def test_unexpected_meter_precheck_errors_still_propagate() -> None:
    called = False

    def dispatch(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": "should not run"}

    store = MemoryStore()
    run = Runtime(store, meters=[BrokenMeter()]).run("broken-meter")
    with pytest.raises(RuntimeError, match="meter configuration failed"):
        run.model_call({"model": "test:model"}, fn=dispatch)

    assert store.children(run.root_id) == []
    assert run.cursor_id == run.root_id
    assert not called


def test_meter_precheck_refusal_rejects_unsafe_audit_metadata() -> None:
    with pytest.raises(ValueError, match="cannot override runtime metadata: created_at"):
        MeterPrecheckRefusal(
            "profile_limit",
            audit_meta={"created_at": "forged"},
        )
    with pytest.raises(ValueError, match="cannot override runtime metadata: charges"):
        MeterPrecheckRefusal(
            "profile_limit",
            audit_meta={"charges": {"tokens": 999}},
        )
    with pytest.raises(TypeError, match="must be JSON serializable"):
        MeterPrecheckRefusal(
            "profile_limit",
            audit_meta={"not_finite": float("nan")},
        )
    with pytest.raises(ValueError, match="requested must be finite"):
        MeterPrecheckRefusal("profile_limit", requested=float("inf"))


def test_mutated_precheck_metadata_cannot_poison_the_charge_ledger() -> None:
    refusal = MeterPrecheckRefusal(
        "profile_limit",
        audit_meta={"diagnostic": {"status": "refused"}},
    )
    assert refusal.audit_meta is not None
    refusal.audit_meta["charges"] = {"tokens": 999}

    class MutatedRefusalMeter:
        name = "tokens"

        def charge(
            self,
            node_kind: str,
            payload: dict[str, object],
            result: object,
            meta: dict[str, object],
        ) -> int:
            del node_kind, payload, result, meta
            return 0

        def precheck_estimate(
            self,
            node_kind: str,
            payload: dict[str, object],
        ) -> None:
            del node_kind, payload
            raise refusal

    called = False

    def dispatch(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"text": "should not run"}

    run = Runtime(MemoryStore(), meters=[MutatedRefusalMeter()]).run("poisoned-meta")
    with pytest.raises(ValueError, match="cannot override runtime metadata: charges"):
        run.model_call({"model": "test:model"}, fn=dispatch)

    assert run.report()["spent"] == {}
    assert run.store.children(run.root_id) == []
    assert not called
