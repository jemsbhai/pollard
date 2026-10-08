"""Seed isolated PyPI stores and verify native environment-selector inspection.

Use only disposable services owned by validate_remote.py. Credentials are passed
through environment variables, never native command arguments or evidence.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from validate_remote import SERVICES, verify_wheel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--services", nargs="+", choices=list(SERVICES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = verify_wheel(args.wheel)
    sys.path.insert(0, str(wheel))
    from pollard import KafkaStore, MongoStore, Neo4jStore, PostgresStore, RedisStore, Runtime, seal

    namespace = "pollard_cli_" + uuid.uuid4().hex
    env = os.environ.copy()
    evidence = {"status": "passed", "namespace": namespace,
                "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(), "checks": {}}
    for service in args.services:
        store_id = namespace + "_" + service
        topic_admin = None
        topic = None
        if service == "postgres":
            factory = lambda: PostgresStore(env["POLLARD_TEST_POSTGRES_DSN"], store_id=store_id)
            selector = "pg-env:POLLARD_TEST_POSTGRES_DSN#" + store_id
        elif service == "redis":
            factory = lambda: RedisStore(env["POLLARD_REDIS_TEST_URL"], store_id=store_id)
            selector = "redis-env:POLLARD_REDIS_TEST_URL?prefix=pollard#" + store_id
        elif service == "mongodb":
            factory = lambda: MongoStore(env["POLLARD_TEST_MONGO_URI"], database=namespace, store_id=store_id, tz_aware=True)
            selector = "mongo-env:POLLARD_TEST_MONGO_URI?database=" + namespace + "&prefix=pollard#" + store_id
        elif service == "neo4j":
            factory = lambda: Neo4jStore(env["POLLARD_TEST_NEO4J_URI"],
                                        auth=(env["POLLARD_TEST_NEO4J_USER"], env["POLLARD_TEST_NEO4J_PASSWORD"]), store_id=store_id)
            selector = "neo4j-env:POLLARD_TEST_NEO4J_URI?user-env=POLLARD_TEST_NEO4J_USER&password-env=POLLARD_TEST_NEO4J_PASSWORD#" + store_id
        else:
            from confluent_kafka.admin import AdminClient, NewTopic
            config = {"bootstrap.servers": env["POLLARD_TEST_KAFKA_BOOTSTRAP"]}
            env["POLLARD_CLI_KAFKA_CONFIG"] = json.dumps(config)
            topic_admin = AdminClient(config)
            topic = namespace + "_kafka"
            topic_admin.create_topics([NewTopic(topic, 1, 1, config={"cleanup.policy": "delete", "retention.ms": "-1", "retention.bytes": "-1"})])[topic].result(timeout=20)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                metadata = topic_admin.list_topics(timeout=2).topics.get(topic)
                if metadata and metadata.error is None and len(metadata.partitions) == 1:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("CLI smoke topic metadata unavailable")
            factory = lambda: KafkaStore(config, topic=topic)
            selector = "kafka-env:POLLARD_CLI_KAFKA_CONFIG?topic=" + topic + "&timeout=10#default"
        try:
            with factory() as store:
                run = Runtime(store).run("cli-smoke")
                root_id = run.root_id
                before = seal(store, root_id).digest
            command = [str(args.binary.resolve()), "runs", selector, "--json"]
            result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            if result.returncode:
                raise RuntimeError(f"{service} CLI inspection failed ({result.returncode}): {result.stderr}")
            rows = json.loads(result.stdout)["runs"]
            assert len(rows) == 1 and rows[0]["root_id"] == root_id and rows[0]["nodes"] == 1, rows
            with factory() as store:
                assert seal(store, root_id).digest == before, "inspection changed recorded tree"
            evidence["checks"][service] = {"root_id": root_id, "command": command,
                "matching_python_root": True, "recorded_tree_unchanged": True, "credentials_in_environment": True}
        finally:
            if topic_admin is not None:
                with contextlib.suppress(Exception):
                    topic_admin.delete_topics([topic])[topic].result(timeout=20)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "checks": list(evidence["checks"])}))


if __name__ == "__main__":
    main()
