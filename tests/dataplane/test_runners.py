import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap_dataplane as bootstrap
import analyze_dataplane
import run_dataplane
from test_archive import functions


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerTests(unittest.TestCase):
    def test_frozen_driver_and_case_correspondence(self):
        records = bootstrap.check_provenance()
        self.assertEqual(len(records[1]['files']), 14)
        for name, item in records[1]['files'].items():
            self.assertEqual(bootstrap.digest(ROOT / name), item['export_sha256'])
            if name.endswith('.py'):
                actual = functions((ROOT / name).read_text())
                for function, expected in item['unchanged_functions_sha256'].items():
                    self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), expected, (name, function))
        retained = records[1]['files']['experiments/dataplane/run.py']['unchanged_functions_sha256']
        for name in ('Case.reactive', 'Case.counter_recovery', 'Case.cli_case', 'Case.finish'):
            self.assertIn(name, retained)
        for name in ('host.py', 'control.py', 'python_driver.py'):
            self.assertTrue(records[1]['files']['experiments/dataplane/' + name]['unchanged'])

    def test_mode_and_selected_profile_counts(self):
        for mode, expected in (('smoke', 13), ('race', 4), ('full', 130)):
            selected = bootstrap.selection(mode)
            self.assertEqual(len(selected['profiles']) * 2 * selected['repetitions'] + selected['cli_repetitions'], expected)
        self.assertEqual(bootstrap.selection('smoke', ['sdk'], True)['cli_repetitions'], 0)
        for profiles in (['sdk', 'sdk'], ['finsy']):
            with self.assertRaises(ValueError):
                bootstrap.selection('race', profiles)

    def test_changed_source_fixture_or_binary_requires_setup(self):
        for changed in ('source.py', 'l2.json', 'driver'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ('source.py', 'l2.json', 'driver'):
                    (root / name).write_text('original')
                record = dict(input_sha256={'source.py': bootstrap.digest(root / 'source.py')},
                              generated_sha256={name: bootstrap.digest(root / name) for name in ('l2.json', 'driver')},
                              source_inventory={}, python_signatures={})
                path = root / 'setup.json'
                path.write_text(json.dumps(record))
                with patch.object(bootstrap, 'ROOT', root), patch.object(bootstrap, 'SETUP', path), \
                     patch.object(bootstrap, 'check_provenance'), patch.object(bootstrap, 'python_signatures', return_value={}):
                    self.assertEqual(bootstrap.check_setup(), record)
                    (root / changed).write_text('changed')
                    with self.assertRaisesRegex(ValueError, 'Data-plane setup changed'):
                        bootstrap.check_setup()

    def test_incomplete_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'manifest.json').write_text(json.dumps(dict(stage='failed')))
            with self.assertRaisesRegex(ValueError, 'Incomplete data-plane run'):
                analyze_dataplane.verify_coverage(root, {})

    def test_failed_child_is_given_time_to_clean_its_network(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [1, 1]
        with tempfile.TemporaryDirectory() as temporary, patch.object(run_dataplane.subprocess, 'Popen', return_value=process), \
             patch.object(run_dataplane.os, 'killpg') as kill:
            with self.assertRaises(subprocess.CalledProcessError):
                run_dataplane.run_logged(['driver'], {}, Path(temporary) / 'run.log')
            process.send_signal.assert_called_once_with(run_dataplane.signal.SIGTERM)
            self.assertEqual(process.wait.call_args_list[1].kwargs, dict(timeout=30))
            kill.assert_called_once_with(12345, run_dataplane.signal.SIGKILL)

    def test_unresponsive_worker_is_killed_and_reaped(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired('driver', 30), -9]
        with tempfile.TemporaryDirectory() as temporary, patch.object(run_dataplane.subprocess, 'Popen', return_value=process), \
             patch.object(run_dataplane.os, 'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                run_dataplane.run_logged(['driver'], {}, Path(temporary) / 'run.log')
            self.assertEqual(process.wait.call_count, 3)
            self.assertEqual(kill.call_count, 2)

    def test_target_startup_failure_stops_the_owned_process(self):
        common = load('dataplane_common_start', bootstrap.DATAPLANE / 'common.py')
        with patch.dict(sys.modules, common=common):
            module = load('dataplane_target_start', bootstrap.DATAPLANE / 'network.py')
        process = MagicMock(returncode=-15, pid=12345)
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, 'unused_port', side_effect=(22001, 22002)), \
             patch.object(module.subprocess, 'Popen', return_value=process), patch.object(module.grpc, 'channel_ready_future') as ready:
            ready.return_value.result.side_effect = TimeoutError('not ready')
            target = module.Target(Path(temporary), ['owned-eth0', 'owned-eth1'])
            with self.assertRaises(TimeoutError):
                target.start()
            process.terminate.assert_called_once()
            self.assertIsNone(target.process)
            self.assertTrue(target.log.closed)
            self.assertIn('exit_code', target.history[0])

    def test_failed_peer_spawn_closes_logs(self):
        module = load('dataplane_peer_spawn', bootstrap.DATAPLANE / 'common.py')
        with tempfile.TemporaryDirectory() as temporary:
            opened, original = [], Path.open
            def opening(path, *args, **kwargs):
                stream = original(path, *args, **kwargs)
                opened.append(stream)
                return stream
            with patch.object(Path, 'open', opening), patch.object(module.subprocess, 'Popen', side_effect=OSError('spawn failed')):
                with self.assertRaises(OSError):
                    module.Peer(['driver'], Path(temporary), 'driver')
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(stream.closed for stream in opened))

    def test_failed_peer_readiness_reaps_a_stalled_process(self):
        module = load('dataplane_peer_readiness', bootstrap.DATAPLANE / 'common.py')
        process = MagicMock(stdin=io.StringIO(), stdout=io.StringIO())
        process.wait.side_effect = [subprocess.TimeoutExpired('driver', 3), -9]
        with tempfile.TemporaryDirectory() as temporary, patch.object(module.Peer, 'wait', side_effect=TimeoutError('not ready')), \
             patch.object(module.subprocess, 'Popen', return_value=process):
            with self.assertRaises(TimeoutError):
                module.Peer(['driver'], Path(temporary), 'driver')
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertTrue(process.stdin.closed and process.stdout.closed)


if __name__ == '__main__':
    unittest.main()
