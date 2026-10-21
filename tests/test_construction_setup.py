import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap_construction as bootstrap


def load_module(name):
    spec = importlib.util.spec_from_file_location("construction_" + name,
                                                 ROOT / "experiments/construction" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConstructionSetupTests(unittest.TestCase):
    def test_exported_sources_and_fixtures_match_inventory(self):
        provenance = bootstrap.check_provenance()
        self.assertEqual(len(provenance["files"]), 24)
        self.assertEqual(sum(item["unchanged"] for item in provenance["files"].values()), 18)
        for identity, expected in provenance["unchanged_functions_sha256"].items():
            filename, function = identity.split(":")
            source = (bootstrap.CONSTRUCTION / filename).read_text()
            node = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == function)
            actual = hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest()
            self.assertEqual(actual, expected, identity)

    def test_fixture_generator_matches_archived_inputs(self):
        fixture = load_module("fixture")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(fixture, "ROOT", Path(temporary)):
                fixture.prepare()
            for name in ("inputs.json", "construct.p4info.txt"):
                self.assertEqual((Path(temporary) / "fixtures" / name).read_bytes(),
                                 (ROOT / "data/construction/fixtures" / name).read_bytes())

    def test_full_schedule_matches_published_run(self):
        runner = load_module("run")
        plan = json.loads((bootstrap.CONSTRUCTION / "formal-plan.json").read_text())
        saved = json.loads((ROOT / "data/construction/run.json").read_text())
        rng = random.Random(plan["seed"])
        schedule = []
        for repetition in range(1, plan["repetitions"] + 1):
            block = runner.jobs(plan)
            rng.shuffle(block)
            schedule.extend(dict(**job, repetition=repetition) for job in block)
        self.assertEqual(schedule, saved["schedule"])

    def test_measurement_environment_keeps_checks_and_clears_overrides(self):
        with patch.dict("os.environ", {"PYTHONOPTIMIZE": "1", "PYTHONPATH": "/other",
                                       "CONSTRUCTION_SAMPLES": "/samples", "GOMAXPROCS": "8"}):
            env = bootstrap.environment()
        for name in ("PYTHONOPTIMIZE", "PYTHONPATH", "CONSTRUCTION_SAMPLES"):
            self.assertNotIn(name, env)
        self.assertEqual(env["GOMAXPROCS"], "1")
        self.assertEqual(env["GOTOOLCHAIN"], "go1.26.9")

    def test_setup_detects_changed_inputs_binary_dependency_and_preflight(self):
        for changed in ("input.py", "driver", ".runtime/sources/client/client.py", "preflight.json"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("input.py", "driver", ".runtime/sources/client/client.py", "preflight.json"):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("original\n")
                setup = dict(input_sha256={"input.py": bootstrap.digest(root / "input.py")},
                             generated_sha256={"driver": bootstrap.digest(root / "driver")},
                             source_inventory={"client": bootstrap.inventory(root / ".runtime/sources/client")},
                             preflight_record="preflight.json", preflight_sha256=bootstrap.digest(root / "preflight.json"))
                setup_path = root / "setup.json"
                setup_path.write_text(json.dumps(setup))
                with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap, "RUNTIME", root / ".runtime"), \
                     patch.object(bootstrap, "SETUP", setup_path), patch.object(bootstrap, "check_provenance"):
                    self.assertEqual(bootstrap.check_setup(), setup)
                    (root / changed).write_text("changed\n")
                    with self.assertRaises(ValueError):
                        bootstrap.check_setup()

    def test_analysis_inventory_keeps_crosscheck_independent(self):
        analysis = load_module("analyze")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "measurement-rounds.csv").write_text("measured values\n")
            (folder / "summary-crosscheck.json").write_text("old independent check\n")
            analysis.record_verification(folder, {"completed_measurements": 27}, [None] * 27)
            record = json.loads((folder / "verification.json").read_text())
            self.assertIn("measurement-rounds.csv", record["files_sha256"])
            self.assertNotIn("summary-crosscheck.json", record["files_sha256"])
            self.assertNotIn("verification.json", record["files_sha256"])
            (folder / "summary-crosscheck.json").write_text("new independent check\n")
            for name, expected in record["files_sha256"].items():
                self.assertEqual(bootstrap.digest(folder / name), expected)

    def test_audit_rejects_stderr_and_changed_metrics(self):
        analysis = load_module("analyze")
        record = json.loads((ROOT / "data/construction/records.jsonl").read_text().splitlines()[0])
        output = (ROOT / "data/construction/stdout/001.txt").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "manifest.json").write_text(json.dumps({"schedule": [record]}))
            (folder / "measurements.jsonl").write_text(json.dumps(record) + "\n")
            (folder / "001-stdout.txt").write_text(output)
            (folder / "001-stderr.txt").write_text("")
            analysis.audit_outputs(folder)
            (folder / "001-stderr.txt").write_text("unexpected output")
            with self.assertRaises(AssertionError):
                analysis.audit_outputs(folder)
            (folder / "001-stderr.txt").write_text("")
            record["metrics"]["elapsed_ns"] += 1
            (folder / "measurements.jsonl").write_text(json.dumps(record) + "\n")
            with self.assertRaises(AssertionError):
                analysis.audit_outputs(folder)


if __name__ == "__main__":
    unittest.main()
