from pathlib import Path
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[1]
PREVIOUS = ROOT
sys.path.insert(0, str(PREVIOUS))
from infra import INFO, CONFIG, Trace, Target, Relay, Session, message, entry, status_record
from session_driver import Driver as SessionDriver

PYTHON = REPOSITORY / ".runtime/finsy/bin/python"
sys.path.insert(0, str(REPOSITORY / "scripts"))
from bootstrap_sessions import check_setup, snapshot_run


def wait_until(function, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        result = function()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError("condition was not observed")
        time.sleep(0.005)


class Driver:
    def __init__(self, adapter, address, log, path="native", delay_ms=0, capacity=50):
        self.log = open(log, "w")
        self.output = Path(log).with_suffix(".driver.jsonl").open("w", buffering=1)
        command = ([os.environ.get("P4EXPERIMENT_SDK_BINARY", str(ROOT / "bin/sdk"))] if adapter == "sdk" else
                   [str(PYTHON), str(ROOT / "finsy_driver.py")])
        command += ["--address", address, "--p4info", str(ROOT / "fixtures/stream.p4info.txt"),
                    "--path", path, "--delay-ms", str(delay_ms), "--capacity", str(capacity)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, bufsize=1)
        self.condition = threading.Condition()
        self.rows = []
        self.next_id = 0
        def reader():
            for line in self.process.stdout:
                self.output.write(line)
                try:
                    row = json.loads(line)
                except ValueError:
                    row = {"kind": "invalid_output", "line": line}
                with self.condition:
                    self.rows.append(row)
                    self.condition.notify_all()
            with self.condition:
                self.rows.append({"kind": "process_ended"})
                self.condition.notify_all()
        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()
        try:
            self.ready = self.wait(lambda row: row.get("kind") == "ready", 20)
        except BaseException:
            self.close()
            raise
        if self.ready.get("error") or not self.ready.get("is_primary"):
            self.close()
            raise RuntimeError(self.ready)

    def wait(self, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                found = next((row for row in self.rows if predicate(row)), None)
                if found is not None:
                    return found
                if any(row.get("kind") in ("process_ended", "invalid_output") for row in self.rows):
                    raise RuntimeError(self.rows[-1])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("driver response was not observed")
                self.condition.wait(min(remaining, .1))

    def command(self, op, **fields):
        self.next_id += 1
        number = self.next_id
        self.process.stdin.write(json.dumps({"id": number, "op": op, **fields}) + "\n")
        self.process.stdin.flush()
        row = self.wait(lambda row: row.get("kind") == "reply" and row.get("id") == number)
        if row.get("fatal"):
            raise RuntimeError(row)
        return row

    def result(self, job, timeout=60):
        return self.wait(lambda row: row.get("kind") == "send_result" and row["job"] == job, timeout)

    def close(self):
        if self.process.poll() is None:
            try:
                self.command("close")
            except (BrokenPipeError, RuntimeError, TimeoutError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()
        self.output.close()
        self.process.stdin.close()
        self.process.stdout.close()


def snapshot(output, configuration):
    digests = snapshot_run(output)
    executable = Path(os.environ.get("P4EXPERIMENT_SDK_BINARY", str(ROOT / "bin/sdk")))
    record = {"date": __import__("datetime").datetime.now().astimezone().isoformat(), **configuration, "files_sha256": digests,
              "sdk_executable": str(executable), "sdk_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")


def verify_snapshot(output):
    record = json.loads((output / "manifest.json").read_text())
    for name, digest in record["files_sha256"].items():
        assert hashlib.sha256((output / "source" / name).read_bytes()).hexdigest() == digest, name
    return record
