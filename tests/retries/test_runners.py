import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap_retries as bootstrap
import analyze_retries
from test_archive import functions
from verify_retries import summaries


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def driver_module():
    infra = SimpleNamespace(INFO=None, CONFIG=None, Target=object, Session=object, entry=object)
    endpoint = SimpleNamespace(Endpoint=object, message=object, binary=object)
    with patch.dict(sys.modules, infra=infra, endpoint=endpoint):
        return load("retry_driver_lifecycle", bootstrap.RETRIES / "run.py")


class RunnerTests(unittest.TestCase):
    def test_frozen_driver_and_trial_correspondence(self):
        records = bootstrap.check_provenance()
        live = records[1]["files"]
        self.assertEqual(len(live), 12)
        for name, item in live.items():
            self.assertEqual(bootstrap.digest(ROOT / name), item["export_sha256"])
            if name.endswith(".py"):
                actual = functions((ROOT / name).read_text())
                for function, expected in item["unchanged_functions_sha256"].items():
                    self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), expected, (name, function))
        self.assertTrue(live["experiments/retries/endpoint.py"]["unchanged"])
        self.assertTrue(live["experiments/retries/python_driver.py"]["unchanged"])
        retained = live["experiments/retries/run.py"]["unchanged_functions_sha256"]
        self.assertTrue({"wire", "snapshot", "trial"} <= set(retained))
        for stack, dependency in (("sdk", "p4runtime-go-controller"), ("legacy", "p4runtime-go-client")):
            text = (bootstrap.RETRIES / ("go-" + stack) / "go.mod").read_text()
            self.assertIn("=> ../../../.runtime/sources/" + dependency + "\n", text)

    def test_modes_preserve_original_case_and_profile_selection(self):
        for mode, expected in (("smoke", [18, 12]), ("race", [20, 12]), ("full", [120, 72])):
            actual = []
            for target in bootstrap.TARGETS:
                plan, cases, profiles, repetitions = bootstrap.selection(target, mode)
                actual.append(len(cases) * len(profiles) * repetitions)
                self.assertEqual(profiles, ["sdk", "legacy"] if mode == "race" else plan["profiles"])
                self.assertEqual(repetitions, 2 if mode == "full" else 1)
            self.assertEqual(actual, expected)

    def test_changed_setup_source_fixture_or_binary_is_rejected(self):
        for changed in ("source.py", "keys.json", "driver"):
            with self.subTest(file=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("source.py", "keys.json", "driver"):
                    (root / name).write_text("original")
                record = dict(input_sha256={"source.py": bootstrap.digest(root / "source.py")},
                              generated_sha256={name: bootstrap.digest(root / name) for name in ("keys.json", "driver")},
                              source_inventory={}, python_signatures={})
                path = root / "setup.json"
                path.write_text(json.dumps(record))
                with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap, "SETUP", path), \
                     patch.object(bootstrap, "check_provenance"), patch.object(bootstrap, "python_signatures", return_value={}):
                    self.assertEqual(bootstrap.check_setup(), record)
                    (root / changed).write_text("changed")
                    with self.assertRaisesRegex(ValueError, "Retry setup changed"):
                        bootstrap.check_setup()

    def test_incomplete_run_cannot_be_analyzed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state.json").write_text(json.dumps(dict(complete=False)))
            with self.assertRaisesRegex(ValueError, "Incomplete retry run"):
                analyze_retries.verify(root)

    def test_race_summary_omits_profiles_that_were_not_run(self):
        rows = [dict(profile="sdk", repetition=1, case_id="all-ok"),
                dict(profile="legacy", repetition=1, case_id="all-ok")]
        trials = [dict(outcome="completed", writes=1, reads=0, attempts={0: 1}, final_state_matches_desired=True)] * 2
        result = dict(counts={}, profile_outcomes={}, trials=trials)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summaries(root, [("controlled", result, rows)])
            with (root / "profile-outcomes.csv").open() as stream:
                profiles = [row["profile"] for row in csv.DictReader(stream)]
            self.assertEqual(profiles, ["sdk", "legacy"])

    def test_failed_spawn_closes_both_driver_logs(self):
        module = driver_module()
        with tempfile.TemporaryDirectory() as temporary, patch.object(module.subprocess, "Popen", side_effect=OSError("spawn failed")):
            folder = Path(temporary)
            opened = []
            original = Path.open
            def opening(path, *args, **kwargs):
                stream = original(path, *args, **kwargs)
                if path.name in ("driver.jsonl", "driver.stderr"):
                    opened.append(stream)
                return stream
            with patch.object(Path, "open", opening):
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    module.Driver("sdk", "127.0.0.1:1", folder, False)
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(stream.closed for stream in opened))

    def test_failed_readiness_kills_and_reaps_driver(self):
        module = driver_module()
        process = MagicMock(stdin=io.StringIO(), stdout=io.StringIO())
        process.poll.return_value = None
        process.wait.return_value = -9
        process.returncode = -9
        with tempfile.TemporaryDirectory() as temporary, patch.object(module.subprocess, "Popen", return_value=process), \
             patch.object(module.Driver, "receive", side_effect=TimeoutError("not ready")):
            with self.assertRaises(TimeoutError):
                module.Driver("sdk", "127.0.0.1:1", Path(temporary), False)
            process.kill.assert_called_once()
            process.wait.assert_called_once()
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def test_broken_shutdown_pipe_still_reaps_driver(self):
        module = driver_module()
        driver = module.Driver.__new__(module.Driver)
        driver.process = MagicMock(returncode=-9)
        driver.process.poll.return_value = None
        driver.process.stdin.flush.side_effect = BrokenPipeError()
        driver.process.stdin.close.side_effect = BrokenPipeError()
        driver.thread = MagicMock()
        driver.thread.is_alive.return_value = False
        driver.stderr, driver.stdout = io.StringIO(), io.StringIO()
        self.assertEqual(driver.close(), -9)
        driver.process.kill.assert_called_once()
        driver.process.wait.assert_called_once()
        self.assertTrue(driver.stderr.closed and driver.stdout.closed)
        driver.process.stdout.close.assert_called_once()

    def test_failed_target_startup_closes_the_owned_process(self):
        read_text, read_bytes = Path.read_text, Path.read_bytes
        info = (ROOT / "data/retries/fixtures/keys.p4info.txt").read_text()
        def text(path, *args, **kwargs):
            return info if path.name == "keys.p4info.txt" else read_text(path, *args, **kwargs)
        def data(path, *args, **kwargs):
            return b"{}" if path.name == "keys.json" else read_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", data):
            module = load("retry_target_lifecycle", bootstrap.RETRIES / "infra.py")
        process = MagicMock(pid=12345, returncode=-15)
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, "unused_port", side_effect=(22001, 22002)), \
             patch.object(module.subprocess, "Popen", return_value=process), patch.object(module.grpc, "channel_ready_future") as ready:
            ready.return_value.result.side_effect = TimeoutError("not ready")
            target = module.Target(Path(temporary))
            with self.assertRaises(TimeoutError):
                target.start()
            process.terminate.assert_called_once()
            self.assertIsNone(target.process)
            self.assertTrue(target.log.closed)
            self.assertIn("exit_code", target.history[0])


if __name__ == "__main__":
    unittest.main()
