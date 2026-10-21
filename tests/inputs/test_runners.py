import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap_inputs as bootstrap
import analyze_inputs


def functions(source):
    result = {}
    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    result[name] = ast.get_source_segment(source, child)
                walk(child, name + ".")
            else:
                walk(child, prefix)
    walk(ast.parse(source))
    return result


class RunnerSetupTests(unittest.TestCase):
    def test_frozen_driver_source_correspondence(self):
        provenance = bootstrap.check_provenance()
        self.assertEqual(len(provenance["files"]), 49)
        for filename, record in provenance["files"].items():
            if filename.endswith(".py"):
                actual = functions((ROOT / filename).read_text())
                for name, expected in record.get("unchanged_functions_sha256", {}).items():
                    self.assertEqual(hashlib.sha256(actual[name].encode()).hexdigest(), expected, (filename, name))

    def test_driver_and_archived_analysis_share_the_same_models(self):
        archived = json.loads((ROOT / "config/input-source-provenance.json").read_text())["files"]
        live = bootstrap.check_provenance()["files"]
        for group in ("boundaries", "constraints"):
            for name in ("model.py", "oracle.py"):
                self.assertEqual(archived[f"scripts/input_checks/{group}/{name}"]["source_sha256"],
                                 live[f"experiments/inputs/{group}/{name}"]["source_sha256"])

    def test_changed_source_fixture_and_binary_require_setup(self):
        for filename in ("source.py", "fixture.json", "driver"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("source.py", "fixture.json", "driver"):
                    (root / name).write_text("original\n")
                setup = dict(input_sha256={"source.py": bootstrap.digest(root / "source.py")},
                             generated_sha256={name: bootstrap.digest(root / name) for name in ("fixture.json", "driver")},
                             source_inventory={}, python_signatures={})
                setup_path = root / "setup.json"
                setup_path.write_text(json.dumps(setup))
                with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap, "SETUP", setup_path), \
                     patch.object(bootstrap, "check_provenance"):
                    self.assertEqual(bootstrap.check_setup(), setup)
                    (root / filename).write_text("changed\n")
                    with self.assertRaisesRegex(ValueError, "setup changed"):
                        bootstrap.check_setup()

    def test_incomplete_run_cannot_be_analyzed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps({"completed": False}))
            with self.assertRaisesRegex(ValueError, "did not complete"):
                analyze_inputs.verify(root, "baseline")

    def test_all_modules_use_repository_local_dependency_paths(self):
        for group in bootstrap.GROUPS:
            for stack, name in (("sdk", "p4runtime-go-controller"), ("legacy", "p4runtime-go-client")):
                text = (bootstrap.INPUTS / group / ("go-" + stack) / "go.mod").read_text()
                self.assertIn("=> ../../../../.runtime/sources/" + name + "\n", text)


class TargetLifecycleTests(unittest.TestCase):
    def test_startup_failure_stops_the_owned_target(self):
        filename = ROOT / "experiments/inputs/common/infra.py"
        spec = importlib.util.spec_from_file_location("input_target_lifecycle", filename)
        module = importlib.util.module_from_spec(spec)
        read_text, read_bytes = Path.read_text, Path.read_bytes
        info = (ROOT / "data/inputs/baseline/fixtures/bmv2.p4info.txt").read_text()
        def text(path, *args, **kwargs):
            return info if path.name == "keys.p4info.txt" else read_text(path, *args, **kwargs)
        def data(path, *args, **kwargs):
            return b"{}" if path.name == "keys.json" else read_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", data):
            spec.loader.exec_module(module)
        process = MagicMock(pid=12345, returncode=-15)
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, "unused_port", side_effect=(22001, 22002)), \
             patch.object(module.subprocess, "Popen", return_value=process), \
             patch.object(module.grpc, "insecure_channel") as channel, \
             patch.object(module.grpc, "channel_ready_future") as ready:
            ready.return_value.result.side_effect = TimeoutError("not ready")
            target = module.Target(Path(temporary))
            with self.assertRaises(TimeoutError):
                target.start()
            process.terminate.assert_called_once()
            process.kill.assert_not_called()
            self.assertIsNone(target.process)
            self.assertTrue(target.log.closed)
            self.assertEqual(target.history[0]["exit_code"], -15)
            channel.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
