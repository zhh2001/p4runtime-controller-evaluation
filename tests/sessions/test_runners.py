import csv
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap_sessions as bootstrap
import analyze_sessions
import run_sessions


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionRunnerTests(unittest.TestCase):
    def test_original_trials_and_driver_source_correspondence(self):
        records = bootstrap.check_provenance()
        live = records[1]["files"]
        self.assertEqual(len(live), 22)
        for name, item in live.items():
            self.assertEqual(bootstrap.digest(ROOT / name), item["export_sha256"])
        for name in ("recover.py", "mastership.py", "consumers.py", "server.py"):
            self.assertTrue(live["experiments/sessions/" + name]["unchanged"], name)
        self.assertIn("trial", live["experiments/sessions/cancellation.py"]["unchanged_functions_sha256"])
        for name in ("sdk", "session-sdk"):
            text = (bootstrap.SESSIONS / name / "go.mod").read_text()
            self.assertIn("=> ../../../.runtime/sources/p4runtime-go-controller\n", text)

    def test_changed_setup_source_fixture_or_binary_is_rejected(self):
        for name in ("driver.py", "keys.json", "sdk"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for filename in ("driver.py", "keys.json", "sdk"):
                    (root / filename).write_text("original")
                record = dict(input_sha256={"driver.py": bootstrap.digest(root / "driver.py")},
                              generated_sha256={filename: bootstrap.digest(root / filename) for filename in ("keys.json", "sdk")},
                              source_inventory={}, python_signatures={})
                setup = root / "setup.json"
                setup.write_text(json.dumps(record))
                with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap, "SETUP", setup), \
                     patch.object(bootstrap, "check_provenance"), patch.object(bootstrap, "python_signatures", return_value={}):
                    self.assertEqual(bootstrap.check_setup(), record)
                    (root / name).write_text("changed")
                    with self.assertRaisesRegex(ValueError, "Session setup changed"):
                        bootstrap.check_setup()

    def test_incomplete_run_cannot_be_analyzed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state.json").write_text(json.dumps(dict(complete=False)))
            with self.assertRaisesRegex(ValueError, "Incomplete session run"):
                analyze_sessions.verify(root)

    def test_cancellation_only_full_run_has_outcomes_without_numeric_summaries(self):
        module = load("session_selected_statistics", bootstrap.SESSIONS / "summarize.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state.json").write_text(json.dumps(dict(complete=True, completed=240, groups=["cancellation"])))
            folder = root / "cancellation"
            folder.mkdir()
            original = ROOT / "data/sessions/cancellation/observations.jsonl.gz"
            (folder / "observations.jsonl").write_bytes(gzip.decompress(original.read_bytes()))
            for name in ("manifest.json", "rpc.jsonl", "verification.json"):
                (folder / name).write_text("{}\n")
            with patch.object(sys, "argv", ["summarize.py", str(root)]), patch("builtins.print"):
                module.main()
            summary = json.loads((root / "summary.json").read_text())
            self.assertEqual((summary["total_trials"], summary["statistics_rows"], len(summary["outcomes"])), (240, 0, 8))
            with (root / "statistics.csv").open() as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 1)
            self.assertIn("median_ci_low", rows[0])

    def test_late_success_is_retained_before_selecting_a_blocked_send(self):
        common = SimpleNamespace(Driver=object, Trace=object, snapshot=object, wait_until=object)
        with patch.dict(sys.modules, common=common, server=SimpleNamespace(Service=object)):
            module = load("session_backpressure_probe", bootstrap.SESSIONS / "cancellation.py")
        driver = MagicMock()
        def result(job, timeout):
            if job == 1 and timeout >= .3:
                return dict(job=1, rpc_code=0)
            raise TimeoutError()
        driver.result.side_effect = result
        blocked, accepted = module.saturate(driver)
        self.assertEqual(blocked, 2)
        self.assertEqual(accepted, [dict(job=1, rpc_code=0)])
        self.assertEqual([item.kwargs["job"] for item in driver.command.call_args_list], [1, 2])

    def test_failed_child_cleans_up_its_process_group(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [1, 1]
        with tempfile.TemporaryDirectory() as temporary, patch.object(run_sessions.subprocess, "Popen", return_value=process), \
             patch.object(run_sessions.os, "killpg") as kill:
            with self.assertRaises(subprocess.CalledProcessError):
                run_sessions.run_logged(["driver"], {}, Path(temporary) / "run.log")
            self.assertEqual(kill.call_args_list, [call(12345, run_sessions.signal.SIGTERM), call(12345, run_sessions.signal.SIGKILL)])

    def test_unresponsive_child_is_killed_before_waiting_again(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [1, subprocess.TimeoutExpired("driver", 5), -9]
        with tempfile.TemporaryDirectory() as temporary, patch.object(run_sessions.subprocess, "Popen", return_value=process), \
             patch.object(run_sessions.os, "killpg") as kill:
            with self.assertRaises(subprocess.CalledProcessError):
                run_sessions.run_logged(["driver"], {}, Path(temporary) / "run.log")
            self.assertEqual(kill.call_args_list[0], call(12345, run_sessions.signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1], call(12345, run_sessions.signal.SIGKILL))
            self.assertEqual(process.wait.call_count, 3)

    def test_failed_target_startup_closes_the_owned_process(self):
        read_text, read_bytes = Path.read_text, Path.read_bytes
        info = (ROOT / "data/sessions/fixtures/keys.p4info.txt").read_text()
        def text(path, *args, **kwargs):
            return info if path.name == "keys.p4info.txt" else read_text(path, *args, **kwargs)
        def data(path, *args, **kwargs):
            return b"{}" if path.name == "keys.json" else read_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", data):
            module = load("session_target_lifecycle", bootstrap.SESSIONS / "infra.py")
        process = MagicMock(pid=12345, returncode=-15)
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, "unused_port", side_effect=(22001, 22002)), \
             patch.object(module.subprocess, "Popen", return_value=process), \
             patch.object(module.grpc, "channel_ready_future") as ready, patch.object(module.grpc, "insecure_channel") as channel:
            ready.return_value.result.side_effect = TimeoutError("not ready")
            target = module.Target(Path(temporary))
            with self.assertRaises(TimeoutError):
                target.start()
            process.terminate.assert_called_once()
            self.assertIsNone(target.process)
            self.assertTrue(target.log.closed)
            self.assertIn("exit_code", target.history[0])
            channel.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
