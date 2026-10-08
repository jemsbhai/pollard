"""Ownership/failure guards for the disposable live-backend runner; no Docker needed."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from validate_remote import OWNERSHIP_LABEL, ROOT, Runner, WHEEL_NAME, checked_python_source, owns_container, test_counts, verify_wheel


class RemoteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = Runner(Path(self.directory.name) / "evidence", ROOT / "crates/pollardai", ["redis"], run_id="owned-run")
        self.runner.created_names = ["candidate"]

    def inspection(self, labels):
        return subprocess.CompletedProcess([], 0, json.dumps([{"Id": "exact-container-id", "Config": {"Labels": labels}}]))

    def test_only_matching_unpredictable_label_authorizes_removal(self):
        for labels in (None, {}, {OWNERSHIP_LABEL: "another-run"}):
            with self.subTest(labels=labels):
                self.runner.docker_probe = Mock(return_value=self.inspection(labels))
                self.runner.cleanup()
                self.runner.docker_probe.assert_called_once_with(["inspect", "candidate"])
                self.assertFalse(self.runner.report["cleanup"][-1]["removed"])
                self.assertFalse(owns_container({"Config": {"Labels": labels}}, "owned-run"))

    def test_removes_exact_inspected_id_and_anonymous_volumes_only(self):
        self.runner.docker_probe = Mock(side_effect=[self.inspection({OWNERSHIP_LABEL: "owned-run"}),
            subprocess.CompletedProcess([], 0, "logs"), subprocess.CompletedProcess([], 0, "removed")])
        self.runner.cleanup()
        self.assertEqual(self.runner.docker_probe.call_args_list[-1].args[0], ["rm", "--force", "--volumes", "exact-container-id"])
        self.assertTrue(self.runner.report["cleanup"][-1]["removed"])

    def test_logging_timeout_does_not_skip_owned_container_cleanup(self):
        self.runner.docker_probe = Mock(side_effect=[self.inspection({OWNERSHIP_LABEL: "owned-run"}),
            subprocess.TimeoutExpired("docker logs", 20), subprocess.CompletedProcess([], 0, "removed")])
        self.runner.cleanup()
        self.assertTrue(self.runner.report["cleanup"][-1]["removed"])
        self.assertIn("log_error", self.runner.report["cleanup"][-1])

    def test_unavailable_docker_is_not_misreported_as_successful_cleanup(self):
        self.runner.docker_probe = Mock(return_value=subprocess.CompletedProcess([], 1, "daemon unavailable"))
        self.runner.cleanup()
        self.assertFalse(self.runner.report["cleanup"][-1]["removed"])

    def test_failed_start_tracks_name_for_owned_cleanup(self):
        self.runner.created_names.clear()
        self.runner.run = Mock(side_effect=RuntimeError("failed startup acknowledgement"))
        with patch("validate_remote.free_port", return_value=12345), self.assertRaises(RuntimeError):
            self.runner.start_service("redis")
        self.assertEqual(self.runner.created_names, ["pollard-parity-owned-run-redis"])

    def test_rejects_corrupt_or_unpinned_wheel(self):
        path = Path(self.directory.name) / WHEEL_NAME
        path.write_bytes(b"untrusted wheel")
        with self.assertRaises(RuntimeError):
            verify_wheel(path)

    def test_zero_tests_is_failure(self):
        with self.assertRaises(RuntimeError):
            test_counts("test result: ok. 0 passed; 0 failed; 7 ignored;")
        self.assertEqual(test_counts("test result: ok. 8 passed; 0 failed; 0 ignored;"), {"passed": 8, "failed": 0, "ignored": 0})

    def test_password_redaction(self):
        self.runner.secrets.append("generated-secret")
        self.assertNotIn("generated-secret", self.runner.redact("password=generated-secret"))

    def test_snapshot_covers_test_assertions_and_release_fixtures(self):
        snapshot = self.runner.source_hashes()
        self.assertIn("crates/pollardai/tests/kafka_live.rs", snapshot)
        self.assertTrue(any(key.startswith("crates/pollardai/tests/") and key.endswith(".json") for key in snapshot))

    def test_corrected_source_requires_the_mongodb_service_and_module(self):
        self.assertIsNone(checked_python_source(None, ["redis"]))
        with self.assertRaises(ValueError):
            checked_python_source(ROOT / "src", ["redis"])
        with self.assertRaises(ValueError):
            checked_python_source(Path(self.directory.name), ["mongodb"])
        self.assertEqual(checked_python_source(ROOT / "src", ["mongodb"]), (ROOT / "src").resolve())

    def test_corrected_python_source_changes_invalidate_the_snapshot(self):
        source = Path(self.directory.name) / "python-source"
        module = source / "pollard/stores/mongodb.py"
        module.parent.mkdir(parents=True)
        module.write_text("original", encoding="utf-8")
        self.runner.python_source = checked_python_source(source, ["mongodb"])
        before = self.runner.source_hashes()
        key = "python-source/pollard/stores/mongodb.py"
        self.assertIn(key, before)
        module.write_text("modified", encoding="utf-8")
        self.assertNotEqual(before[key], self.runner.source_hashes()[key])

    def test_timeout_stops_child_and_grandchild(self):
        marker = Path(self.directory.name) / "heartbeat"
        child = "import sys,time;from pathlib import Path;p=Path(sys.argv[1]);\nwhile True:p.write_text(str(time.monotonic()));time.sleep(.05)"
        parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
        with self.assertRaises(RuntimeError):
            self.runner.run("expected-timeout", [sys.executable, "-c", parent, child, str(marker)], timeout=2)
        self.assertTrue(marker.exists(), "grandchild did not begin before timeout")
        settled = marker.read_text()
        time.sleep(0.3)
        self.assertEqual(marker.read_text(), settled, "grandchild survived timeout")
        self.assertEqual(self.runner.report["commands"][-1]["exit_code"], -1)


if __name__ == "__main__":
    unittest.main()
