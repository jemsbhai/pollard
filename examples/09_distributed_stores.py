"""Record, replay, verify, and seal a run in a configured remote store."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from pollard import (
    Budget,
    BudgetExceeded,
    KafkaStore,
    MongoStore,
    Neo4jStore,
    PostgresStore,
    RedisStore,
    ReplayMode,
    Runtime,
    seal,
    verify,
)
from pollard.arbiter import TransactionalArbiter
from pollard.meters import StepMeter

BACKENDS = ("postgresql", "redis", "mongodb", "neo4j", "kafka")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is absent: {name}")
    return value


def _open_store(backend: str, store_id: str) -> Any:
    if backend == "postgresql":
        return PostgresStore(_required_environment("POLLARD_PG_DSN"), store_id=store_id)
    if backend == "redis":
        return RedisStore(
            _required_environment("POLLARD_REDIS_URL"),
            store_id=store_id,
            prefix=os.environ.get("POLLARD_REDIS_PREFIX", "pollard"),
        )
    if backend == "mongodb":
        return MongoStore(
            _required_environment("POLLARD_MONGODB_URI"),
            database=os.environ.get("POLLARD_MONGODB_DATABASE", "pollard"),
            store_id=store_id,
        )
    if backend == "neo4j":
        return Neo4jStore(
            _required_environment("POLLARD_NEO4J_URI"),
            (
                os.environ.get("POLLARD_NEO4J_USER", "neo4j"),
                _required_environment("POLLARD_NEO4J_PASSWORD"),
            ),
            database=os.environ.get("POLLARD_NEO4J_DATABASE", "neo4j"),
            store_id=store_id,
        )
    if backend == "kafka":
        return KafkaStore(
            {
                "bootstrap.servers": _required_environment(
                    "POLLARD_KAFKA_BOOTSTRAP"
                )
            },
            topic=_required_environment("POLLARD_KAFKA_TOPIC"),
            store_id=store_id,
        )
    raise ValueError(f"unsupported backend: {backend}")


def _offline_result(_payload: object) -> dict[str, object]:
    return {"text": "stored without a model-provider request"}


def _unexpected_live_call(_payload: object) -> dict[str, object]:
    raise AssertionError("strict replay executed the sentinel callable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use one configured remote store for an offline record/replay check. "
            "The command contacts only the selected database or broker."
        )
    )
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    parser.add_argument("--store-id", default="pollard-example")
    parser.add_argument("--run-label", default="distributed-store-example")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        store = _open_store(args.backend, args.store_id)
    except (ImportError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    try:
        with store:
            payload = {"model": "offline", "purpose": "distributed-store-example"}
            with Runtime(store, meters=[StepMeter()]).run(
                args.run_label,
                budget=Budget(steps=1),
            ) as run:
                node = run.model_call(payload, fn=_offline_result)
                root_id = run.root_id

            with Runtime(
                store,
                meters=[StepMeter()],
                mode=ReplayMode.REPLAY,
            ).run(args.run_label, budget=Budget(steps=1)) as replay:
                replayed = replay.model_call(payload, fn=_unexpected_live_call)

            report = verify(store, root_id)
            sealed = seal(store, root_id)
            print(
                json.dumps(
                    {
                        "backend": args.backend,
                        "node_id": node.id,
                        "replay_matched": replayed.result == node.result,
                        "root_id": root_id,
                        "seal_digest": sealed.digest,
                        "seal_entries": len(sealed.entries),
                        "shared_arbiter": isinstance(store, TransactionalArbiter),
                        "verified": report.ok,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    except BudgetExceeded as exc:
        parser.error(
            f"{exc}; this run label has already spent its one-step budget. "
            "Use --run-label with a fresh value for a new recording."
        )


if __name__ == "__main__":
    main()
