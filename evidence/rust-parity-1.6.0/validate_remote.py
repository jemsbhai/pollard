"""Reproduce live native/PyPI backend validation with runner-owned Docker services.

Requires native Rust/Cargo, Docker running Linux containers, CMake and Python3.10+.
The default creates a private Python environment and downloads a SHA-pinned wheel.
Only containers carrying this invocation's unpredictable ownership label are
removed; existing services, images, named volumes and user databases are untouched.
Run from any directory; --help lists service selection and evidence options.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import venv

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WHEEL_NAME = "pollard-1.6.0-py3-none-any.whl"
WHEEL_SHA256 = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
OWNERSHIP_LABEL = "pollard.parity.run"
SERVICES = {
    "postgres": {"image": "postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
                 "port": 5432, "test": "postgres_parity", "driver": "psycopg[binary]==3.3.4"},
    "redis": {"image": "redis:7.4-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
              "port": 6379, "test": "redis_store", "driver": "redis==7.2.1"},
    "kafka": {"image": "docker.redpanda.com/redpandadata/redpanda:v24.1.21@sha256:dc33c62306c742c0d8850c883c39bea9ab5351c32a93e0d9fa560ae04b43a3c0",
              "port": 9092, "test": "kafka_live", "driver": "confluent-kafka==2.15.0"},
    "mongodb": {"image": "mongo:7@sha256:1f995ad6fdb93244a1addab1b58f934a0bc2f5643c38e02f5e9d7f0c7d227a7b",
                "port": 27017, "test": "mongodb_parity", "driver": "pymongo==4.16.0"},
    "neo4j": {"image": "neo4j:5.26-community@sha256:c7d25c0eeebfe125718d58b72ed5d663c85d0479047733845eb2237c67ce8069",
              "port": 7687, "test": "neo4j_parity", "driver": "neo4j==6.2.0"},
}
TEST_RESULT = re.compile(r"test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored;")


def stop_process_tree(process: subprocess.Popen) -> None:
    """Terminate only the process tree/group created for this command."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def verify_wheel(path: Path) -> Path:
    if path.name != WHEEL_NAME or hashlib.sha256(path.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise RuntimeError("release oracle must be the SHA-pinned Pollard1.6.0 wheel")
    return path.resolve()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def owns_container(info: dict, run_id: str) -> bool:
    return (info.get("Config", {}).get("Labels") or {}).get(OWNERSHIP_LABEL) == run_id


def test_counts(output: str) -> dict:
    counts = [tuple(map(int, row)) for row in TEST_RESULT.findall(output)]
    if not counts or sum(row[0] for row in counts) == 0:
        raise RuntimeError("live test command did not report any passing tests")
    return dict(zip(("passed", "failed", "ignored"), (sum(row[i] for row in counts) for i in range(3))))


def checked_python_source(source: Path | None, services: list[str]) -> Path | None:
    if source is None:
        return None
    source = source.resolve()
    if "mongodb" not in services or not (source / "pollard/stores/mongodb.py").is_file():
        raise ValueError("--python-source requires mongodb and a source directory containing pollard/stores/mongodb.py")
    return source


class Runner:
    def __init__(self, output: Path, crate: Path, services: list[str], *, run_id: str | None = None):
        self.output = output.resolve()
        self.crate = crate.resolve()
        self.services = services
        self.python_source: Path | None = None
        self.run_id = run_id or uuid.uuid4().hex
        self.created_names: list[str] = []
        self.secrets: list[str] = []
        self.env = {**os.environ, "CARGO_TERM_COLOR": "never", "RUST_BACKTRACE": "1"}
        self.report = {"status": "running", "run_id": self.run_id, "pollard_version": "1.6.0",
                       "wheel_sha256": WHEEL_SHA256, "services": services,
                       "commands": [], "containers": [], "cleanup": []}
        if self.output.exists() and any(self.output.iterdir()):
            raise RuntimeError(f"use a fresh evidence directory: {self.output}")
        (self.output / "logs").mkdir(parents=True, exist_ok=True)
        (self.output / "results").mkdir()

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "<generated-test-password>")
        return text

    def run(self, name: str, command: list[str], *, timeout: float = 1800, check: bool = True) -> str:
        command = [str(arg) for arg in command]
        print(f"[{name}] {self.redact(' '.join(command))}", flush=True)
        start = time.monotonic()
        interrupted = False
        try:
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(command, cwd=ROOT, env=self.env, text=True, encoding="utf-8",
                                       errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
            try:
                output, _ = process.communicate(timeout=timeout)
                code = process.returncode
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                interrupted = isinstance(error, KeyboardInterrupt)
                stop_process_tree(process)
                output, _ = process.communicate(timeout=15)
                code = -2 if interrupted else -1
                output += "\ncommand interrupted; process tree terminated\n" if interrupted else f"\ncommand exceeded {timeout}s timeout; process tree terminated\n"
        except OSError as error:
            code, output = -1, str(error)
        path = self.output / "logs" / f"{len(self.report['commands']):02}-{name}.log"
        path.write_text(self.redact(output), encoding="utf-8")
        entry = {"name": name, "command": [self.redact(arg) for arg in command], "exit_code": code,
                 "elapsed_seconds": round(time.monotonic() - start, 3), "log": str(path.relative_to(self.output))}
        self.report["commands"].append(entry)
        if interrupted:
            raise KeyboardInterrupt()
        if check and code != 0:
            raise RuntimeError(f"{name} failed ({code}); see {path}\n{self.redact(output[-5000:])}")
        return output

    def docker_probe(self, command: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *command], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)

    def wait_ready(self, name: str, probe: list[str], *, timeout: float = 180) -> None:
        start = time.monotonic()
        last = ""
        attempts = 0
        while time.monotonic() - start < timeout:
            attempts += 1
            try:
                result = self.docker_probe(probe)
                last = result.stdout
                if result.returncode == 0:
                    self.report["containers"][-1]["readiness"] = {"attempts": attempts, "seconds": round(time.monotonic()-start, 3)}
                    return
            except subprocess.TimeoutExpired:
                last = "readiness command timed out"
            time.sleep(0.5)
        raise RuntimeError(f"{name} did not become ready: {self.redact(last[-2000:])}")

    def start_service(self, service: str) -> None:
        config = SERVICES[service]
        port = free_port()
        name = f"pollard-parity-{self.run_id[:12]}-{service}"
        password = secrets.token_urlsafe(24)
        self.secrets.append(password)
        options: list[str] = []
        arguments: list[str] = []
        if service == "postgres":
            options = ["-e", "POSTGRES_USER=pollard", "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=pollard_test"]
            self.env["POLLARD_TEST_POSTGRES_DSN"] = f"host=127.0.0.1 port={port} user=pollard password={password} dbname=pollard_test"
            ready = ["exec", name, "pg_isready", "-U", "pollard", "-d", "pollard_test"]
        elif service == "redis":
            self.env["POLLARD_REDIS_TEST_URL"] = f"redis://127.0.0.1:{port}/"
            ready = ["exec", name, "redis-cli", "PING"]
        elif service == "kafka":
            arguments = ["redpanda", "start", "--overprovisioned", "--smp", "1", "--memory", "512M", "--reserve-memory", "0M",
                         "--node-id", "0", "--check=false", "--kafka-addr", "PLAINTEXT://0.0.0.0:9092",
                         "--advertise-kafka-addr", f"PLAINTEXT://127.0.0.1:{port}", "--rpc-addr", "0.0.0.0:33145",
                         "--advertise-rpc-addr", "127.0.0.1:33145"]
            self.env["POLLARD_TEST_KAFKA_BOOTSTRAP"] = f"127.0.0.1:{port}"
            ready = ["exec", name, "rpk", "cluster", "info", "-X", "brokers=127.0.0.1:9092"]
        elif service == "mongodb":
            arguments = ["--replSet", "rs0", "--bind_ip_all", "--wiredTigerCacheSizeGB", "0.25"]
            self.env["POLLARD_TEST_MONGO_URI"] = f"mongodb://127.0.0.1:{port}/?replicaSet=rs0&directConnection=true"
            ready = ["exec", name, "mongosh", "--quiet", "--eval", "quit(db.adminCommand({ping:1}).ok ? 0 : 1)"]
        else:
            options = ["-e", f"NEO4J_AUTH=neo4j/{password}", "-e", f"NEO4J_server_bolt_advertised__address=127.0.0.1:{port}",
                       "-e", "NEO4J_server_memory_heap_initial__size=256m", "-e", "NEO4J_server_memory_heap_max__size=512m",
                       "-e", "NEO4J_server_memory_pagecache_size=128m"]
            self.env.update(POLLARD_TEST_NEO4J_URI=f"neo4j://127.0.0.1:{port}", POLLARD_TEST_NEO4J_USER="neo4j",
                            POLLARD_TEST_NEO4J_PASSWORD=password)
            ready = ["exec", name, "cypher-shell", "-u", "neo4j", "-p", password, "RETURN 1"]
        # Track the intended name before Docker starts: even a failed/interrupting
        # run can leave a created container. Cleanup still requires our label.
        self.created_names.append(name)
        self.run(f"start-{service}", ["docker", "run", "--detach", "--name", name,
                 "--label", f"{OWNERSHIP_LABEL}={self.run_id}", "--label", "pollard.parity.task=rust-pypi-parity",
                 "--label", f"pollard.parity.service={service}", "--publish", f"127.0.0.1:{port}:{config['port']}",
                 *options, config["image"], *arguments], timeout=600)
        info = json.loads(self.docker_probe(["inspect", name]).stdout)[0]
        if not owns_container(info, self.run_id):
            raise RuntimeError("Docker ownership label mismatch after startup")
        self.report["containers"].append({"service": service, "name": name, "id": info["Id"],
                                          "image": config["image"], "image_id": info["Image"], "host_port": port})
        self.wait_ready(service, ready)
        if service == "mongodb":
            self.run("mongo-replica-init", ["docker", "exec", name, "mongosh", "--quiet", "--eval",
                     'rs.initiate({_id:"rs0",members:[{_id:0,host:"127.0.0.1:27017"}]})'], timeout=30)
            self.wait_ready(service, ["exec", name, "mongosh", "--quiet", "--eval",
                                     "quit(db.hello().isWritablePrimary ? 0 : 1)"])

    def cleanup(self) -> None:
        for name in reversed(self.created_names):
            entry = {"name": name, "removed": False}
            try:
                result = self.docker_probe(["inspect", name])
                if result.returncode != 0:
                    if "No such" in result.stdout:
                        entry.update(removed=True, already_absent=True)
                    else:
                        entry["error"] = self.redact(result.stdout)
                    self.report["cleanup"].append(entry)
                    continue
                info = json.loads(result.stdout)[0]
                if not owns_container(info, self.run_id):
                    entry["error"] = "ownership label mismatch; container preserved"
                else:
                    container_id = info["Id"]
                    try:
                        logs = self.docker_probe(["logs", container_id])
                        (self.output / "logs" / f"container-{name}.log").write_text(self.redact(logs.stdout), encoding="utf-8")
                    except Exception as error:
                        entry["log_error"] = self.redact(str(error))
                    result = self.docker_probe(["rm", "--force", "--volumes", container_id], timeout=60)
                    entry.update(id=container_id, removed=result.returncode == 0)
                    if result.returncode:
                        entry["error"] = self.redact(result.stdout)
            except Exception as error:
                entry["error"] = self.redact(str(error))
            self.report["cleanup"].append(entry)

    def wheel(self, supplied: Path | None) -> Path:
        if supplied:
            return verify_wheel(supplied)
        destination = self.output / WHEEL_NAME
        with urllib.request.urlopen("https://pypi.org/pypi/pollard/1.6.0/json", timeout=30) as response:
            metadata = json.load(response)
        artifact = next(row for row in metadata["urls"] if row["filename"] == WHEEL_NAME)
        if artifact["digests"]["sha256"] != WHEEL_SHA256:
            raise RuntimeError("PyPI release digest differs from pinned oracle")
        with urllib.request.urlopen(artifact["url"], timeout=60) as response:
            destination.write_bytes(response.read())
        return verify_wheel(destination)

    def python(self, use_current: bool) -> Path:
        if use_current:
            executable = Path(sys.executable)
        else:
            directory = self.output / ".venv"
            venv.EnvBuilder(with_pip=True).create(directory)
            executable = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            self.run("python-drivers", [str(executable), "-m", "pip", "install", "--disable-pip-version-check",
                     *[SERVICES[service]["driver"] for service in self.services]], timeout=600)
        packages = self.run("python-environment", [str(executable), "-m", "pip", "list", "--format=json"])
        (self.output / "python-environment.json").write_text(packages, encoding="utf-8")
        return executable

    def cargo(self, operation: str, *args: str) -> list[str]:
        return ["cargo", operation, "--locked", "--manifest-path", str(self.crate / "Cargo.toml"),
                "--features", ",".join(self.services), *args]

    def source_hashes(self) -> dict[str, str]:
        files = list((self.crate / "src").rglob("*.rs")) + list((self.crate / "examples").glob("*.rs"))
        files += [path for path in (self.crate / "tests").rglob("*") if path.suffix in (".rs", ".json", ".py")]
        files += [self.crate / "Cargo.toml", self.crate / "Cargo.lock"] + list(HERE.glob("*.py"))
        hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(files)}
        if self.python_source is not None:
            hashes.update({"python-source/" + path.relative_to(self.python_source).as_posix():
                           hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((self.python_source / "pollard").rglob("*.py"))})
        return hashes

    def validate(self, wheel: Path | None, use_current_python: bool, python_source: Path | None = None) -> None:
        self.python_source = checked_python_source(python_source, self.services)
        self.report["rustc"] = self.run("rustc-version", ["rustc", "--version"]).strip()
        self.report["docker"] = self.run("docker-version", ["docker", "version", "--format", "{{json .Server}}"], timeout=30).strip()
        wheel = self.wheel(wheel)
        python = self.python(use_current_python)
        self.report["source_sha256"] = self.source_hashes()
        metadata = json.loads(self.run("cargo-metadata", ["cargo", "metadata", "--locked", "--no-deps", "--format-version", "1",
                                                           "--manifest-path", str(self.crate / "Cargo.toml")]))
        examples = [f"{service}_interop" for service in self.services if service in ("postgres", "redis", "kafka")]
        if any(service in self.services for service in ("mongodb", "neo4j")):
            examples.append("document_interop")
        build_args = [arg for name in examples for arg in ("--example", name)]
        self.run("build-native-interop", self.cargo("build", "--bin", "pollard", *build_args))
        executable_dir = Path(metadata["target_directory"]) / "debug/examples"
        binaries = {name: executable_dir / (name + (".exe" if os.name == "nt" else "")) for name in examples}
        cli = executable_dir.parent / ("pollard.exe" if os.name == "nt" else "pollard")
        # Cargo's shared target paths can change during independent feature-matrix
        # builds. Freeze these artifacts before any live process starts.
        snapshot = self.output / "bin"
        snapshot.mkdir()
        for name, path in list(binaries.items()):
            binaries[name] = Path(shutil.copy2(path, snapshot / path.name))
        cli = Path(shutil.copy2(cli, snapshot / cli.name))
        self.report["binary_sha256"] = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
        self.report["binary_sha256"]["pollard"] = hashlib.sha256(cli.read_bytes()).hexdigest()
        for service in self.services:
            self.start_service(service)
        for service in self.services:
            output = self.run(f"test-{service}", self.cargo("test", "--test", SERVICES[service]["test"], "--",
                              "--include-ignored" if service == "redis" else "--ignored", "--test-threads=1"))
            self.report["commands"][-1]["tests"] = test_counts(output)
            if service == "postgres":
                output = self.run("test-postgres-lost-ack", self.cargo("test", "--lib", "live_lost_commit_ack", "--", "--ignored"))
                self.report["commands"][-1]["tests"] = test_counts(output)
        results = self.output / "results"
        for service in self.services:
            result = results / f"{service}-interop.json"
            if service == "postgres":
                command = [python, HERE / "postgres_interop.py", "--wheel", wheel, "--binary", binaries["postgres_interop"], "--output", result]
            elif service == "redis":
                command = [python, HERE / "redis_interop.py", wheel, binaries["redis_interop"]]
            elif service == "kafka":
                command = [python, HERE / "kafka_interop.py", "--wheel", wheel, "--crate", self.crate,
                           "--binary", binaries["kafka_interop"],
                           "--bootstrap", self.env["POLLARD_TEST_KAFKA_BOOTSTRAP"], "--output", result]
            else:
                command = [python, HERE / "document_interop.py", "--wheel", wheel, "--binary", binaries["document_interop"],
                           "--backend", service, "--output", result]
            output = self.run(f"interop-{service}", command, timeout=600)
            if service == "redis":
                json.loads(output)
                result.write_text(output, encoding="utf-8")
            elif not result.is_file():
                raise RuntimeError(f"{service} interoperability did not write its evidence")
            json.loads(result.read_text("utf-8"))
        if self.python_source is not None:
            result = results / "mongodb-corrected-source-interop.json"
            self.run("interop-mongodb-corrected-source", [python, HERE / "document_interop.py", "--wheel", wheel,
                     "--binary", binaries["document_interop"], "--backend", "mongodb", "--output", result,
                     "--python-source", self.python_source], timeout=600)
            companion = json.loads(result.read_text("utf-8"))
            if companion.get("python_origin", {}).get("kind") != "corrected-source":
                raise RuntimeError("corrected-source MongoDB check did not record independent source provenance")
        self.run("cli-remote-inspection", [python, HERE / "remote_cli_smoke.py", "--wheel", wheel,
                 "--binary", cli, "--services", *self.services, "--output", results / "cli-remote-inspection.json"], timeout=600)
        after = self.source_hashes()
        self.report["source_changed_during_run"] = self.report["source_sha256"] != after
        if self.report["source_changed_during_run"]:
            self.report["source_sha256_after"] = after
            raise RuntimeError("source changed during validation; rerun against a frozen checkout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crate", type=Path, default=ROOT / "crates/pollardai")
    parser.add_argument("--wheel", type=Path, help="verified local wheel; otherwise download and hash-check PyPI1.6.0")
    parser.add_argument("--services", nargs="+", choices=list(SERVICES), default=list(SERVICES))
    parser.add_argument("--output-dir", type=Path, help="fresh directory for JSON, logs and private Python environment")
    parser.add_argument("--use-current-python", action="store_true", help="use installed drivers; do not create/install a private venv")
    parser.add_argument("--python-source", type=Path, help="also validate corrected Python MongoDB defaults; retains every frozen-release oracle check")
    args = parser.parse_args()
    services = list(dict.fromkeys(args.services))
    try:
        python_source = checked_python_source(args.python_source, services)
    except ValueError as error:
        parser.error(str(error))
    run_id = uuid.uuid4().hex
    output = args.output_dir or ROOT / ".benchmarks/remote-validation" / run_id
    runner = Runner(output, args.crate, services, run_id=run_id)
    started = time.monotonic()
    try:
        runner.validate(args.wheel, args.use_current_python, python_source)
        runner.report["status"] = "passed"
    except BaseException as error:
        runner.report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        runner.report["error"] = runner.redact(str(error))
        print(runner.report["error"], file=sys.stderr, flush=True)
    finally:
        runner.cleanup()
        if any(not item["removed"] for item in runner.report["cleanup"]):
            runner.report["status"] = "failed"
        runner.report["elapsed_seconds"] = round(time.monotonic()-started, 3)
        report_path = runner.output / "summary.json"
        report_path.write_text(json.dumps(runner.report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        print(json.dumps({"status": runner.report["status"], "summary": str(report_path)}), flush=True)
    return 0 if runner.report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
