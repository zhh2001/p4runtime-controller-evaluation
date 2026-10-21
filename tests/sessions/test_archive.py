import ast
import contextlib
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_sessions as verify
from session_checks import extra_checks, secondary


def archived(group, name):
    path = ROOT / "data/sessions" / group / (name + ".jsonl.gz")
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]


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


class InventoryTests(unittest.TestCase):
    def copy_archive(self, root):
        for name in ("data/sessions", "figures/sessions"):
            shutil.copytree(ROOT / name, root / name)

    def test_complete_inventory_and_original_record_bytes(self):
        manifest = verify.verify_inventory(ROOT)
        self.assertEqual(len(manifest["files"]), 42)
        self.assertEqual(sum(run["trials"] for run in manifest["runs"]), 840)
        for name, item in manifest["files"].items():
            if name.endswith(("observations.jsonl.gz", "rpc.jsonl.gz")):
                self.assertEqual(item["source_sha256"], item["uncompressed_sha256"])

    def test_analysis_function_and_original_test_correspondence(self):
        provenance = json.loads((ROOT / "config/session-source-provenance.json").read_text())
        self.assertEqual(len(provenance["files"]), 8)
        for name, item in provenance["files"].items():
            actual = functions((ROOT / name).read_text())
            for function, expected in item.get("unchanged_functions_sha256", {}).items():
                self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), expected, (name, function))
            for function, original in item.get("extracted_functions", {}).items():
                self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), original["function_sha256"])

    def test_missing_inventory_item_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.copy_archive(root)
            path = root / "data/sessions/manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"].pop("data/sessions/recovery/rpc.jsonl.gz")
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "file inventory"):
                verify.verify_inventory(root)

    def test_unlisted_archive_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.copy_archive(root)
            (root / "data/sessions/extra.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Unexpected E3 archive file"):
                verify.verify_inventory(root)

    def test_changed_compressed_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.copy_archive(root)
            path = root / "data/sessions/recovery/rpc.jsonl.gz"
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify.verify_inventory(root)

    def test_changed_uncompressed_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.copy_archive(root)
            path = root / "data/sessions/manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"]["data/sessions/recovery/rpc.jsonl.gz"]["uncompressed_sha256"] = "0" * 64
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Uncompressed record"):
                verify.verify_inventory(root)


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads((ROOT / "data/sessions/plan.json").read_text())
        cls.metadata = json.loads((ROOT / "data/sessions/recovery/run.json").read_text())
        cls.rows = archived("recovery", "observations")
        cls.events = archived("recovery", "rpc")

    def check(self, rows=None, events=None, run=None):
        verify.verify_coverage(self.rows if rows is None else rows, self.events if events is None else events,
                               self.metadata if run is None else run, self.plan)

    def test_all_formal_groups_have_exact_coverage_and_schedule(self):
        for group in verify.GROUPS:
            with self.subTest(group=group):
                run = json.loads((ROOT / "data/sessions" / group / "run.json").read_text())
                verify.verify_coverage(archived(group, "observations"), archived(group, "rpc"), run, self.plan)

    def test_missing_trial_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing trials"):
            self.check(rows=self.rows[:-1])

    def test_duplicate_trial_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Trial identity"):
            self.check(rows=[self.rows[0], *self.rows[:-1]])

    def test_changed_randomized_schedule_is_rejected(self):
        run = copy.deepcopy(self.metadata)
        run["schedule"][0], run["schedule"][1] = run["schedule"][1], run["schedule"][0]
        with self.assertRaisesRegex(ValueError, "randomized schedule"):
            self.check(run=run)

    def test_duplicate_or_missing_rpc_event_is_rejected(self):
        for events in (self.events[:-1], [self.events[0], *self.events[:-1]]):
            with self.subTest(size=len(events)), self.assertRaises(ValueError):
                self.check(events=events)

    def test_trial_event_identity_is_checked(self):
        events = list(self.events)
        events[0] = dict(events[0], adapter="sdk")
        with self.assertRaisesRegex(ValueError, "Trial event markers"):
            self.check(events=events)

    def test_consumer_queue_layers_are_checked(self):
        group = "consumers"
        run = json.loads((ROOT / "data/sessions" / group / "run.json").read_text())
        rows = archived(group, "observations")
        events = archived(group, "rpc")
        index = next(i for i, row in enumerate(rows) if row["path"] == "native")
        rows[index]["application_capacity"] = 50
        with self.assertRaisesRegex(ValueError, "queue capacity"):
            verify.verify_coverage(rows, events, run, self.plan)


class RawEventTests(unittest.TestCase):
    def test_secondary_decoder_detects_changed_demotion_duration(self):
        row = copy.deepcopy(archived("mastership", "observations")[0])
        row["demotion_to_observation_ns"] += 1
        run = json.loads((ROOT / "data/sessions/mastership/run.json").read_text())
        run.update(files_sha256={}, schedule=[{key: row[key] for key in ("adapter", "repetition")}],
                   repetitions=1, trials=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps(run))
            (root / "observations.jsonl").write_text(json.dumps(row) + "\n")
            (root / "rpc.jsonl").write_text("".join(json.dumps(event) + "\n" for event in archived("mastership", "rpc")))
            with self.assertRaises(AssertionError):
                secondary.check_group(root)

    def test_extra_write_is_detected_from_raw_rpc_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for group in ("recovery", "mastership"):
                folder = root / group
                folder.mkdir()
                row = archived(group, "observations")[0]
                events = archived(group, "rpc")
                if group == "mastership":
                    index = row["backup_observed_sequence"]
                    events[index] = dict(row["writes"][0], sequence=index)
                (folder / "observations.jsonl").write_text(json.dumps(row) + "\n")
                (folder / "rpc.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
            with patch.object(sys, "argv", ["extra_checks.py", str(root)]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(AssertionError):
                    extra_checks.main()


if __name__ == "__main__":
    unittest.main()
