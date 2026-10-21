import ast
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_retries as verify
from retry_checks import analyze, secondary


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
    def test_complete_inventory_and_original_bytes(self):
        manifest = verify.verify_inventory(ROOT)
        self.assertEqual(len(manifest["files"]), 23)
        self.assertEqual(sum(run["trials"] for run in manifest["runs"]), 192)

    def test_original_function_and_test_correspondence(self):
        provenance = json.loads((ROOT / "config/retry-source-provenance.json").read_text())
        self.assertEqual(len(provenance["files"]), 5)
        for name, item in provenance["files"].items():
            actual = functions((ROOT / name).read_text())
            for function, expected in item["unchanged_functions_sha256"].items():
                self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), expected, (name, function))
        tests = provenance["files"]["tests/retries/test_policy.py"]["unchanged_functions_sha256"]
        self.assertEqual(sum(".test_" in name for name in tests), 13)

    def mutate(self, action):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "data/retries", root / "data/retries")
            action(root)
            verify.verify_inventory(root)

    def test_missing_inventory_entry_is_rejected(self):
        def action(root):
            path = root / "data/retries/manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"].pop("data/retries/controlled/rpc.jsonl.gz")
            path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "file inventory"):
            self.mutate(action)

    def test_unlisted_archive_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected E4 archive"):
            self.mutate(lambda root: (root / "data/retries/extra.json").write_text("{}"))

    def test_changed_compressed_bytes_are_rejected(self):
        def action(root):
            path = root / "data/retries/controlled/rpc.jsonl.gz"
            path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.mutate(action)

    def test_changed_uncompressed_digest_is_rejected(self):
        def action(root):
            path = root / "data/retries/manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"]["data/retries/controlled/rpc.jsonl.gz"]["uncompressed_sha256"] = "0" * 64
            path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Uncompressed record"):
            self.mutate(action)


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads((ROOT / "data/retries/plan.json").read_text())
        cls.corpus = json.loads((ROOT / "data/retries/fixtures/cases.json").read_text())
        cls.metadata = json.loads((ROOT / "data/retries/controlled/run.json").read_text())
        cls.rows = verify.archived(ROOT, "controlled", "observations")
        cls.records = verify.archived(ROOT, "controlled", "rpc")

    def check(self, rows=None, records=None, run=None):
        verify.verify_coverage(self.rows if rows is None else rows, self.records if records is None else records,
                               self.metadata if run is None else run, self.plan, self.corpus)

    def test_both_formal_runs_have_complete_coverage(self):
        for mode in verify.MODES:
            run = json.loads((ROOT / "data/retries" / mode / "run.json").read_text())
            verify.verify_coverage(verify.archived(ROOT, mode, "observations"), verify.archived(ROOT, mode, "rpc"),
                                   run, self.plan, self.corpus)

    def test_missing_trial_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing trials"):
            self.check(rows=self.rows[:-1])

    def test_duplicate_or_reordered_trial_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Trial identity"):
            self.check(rows=[self.rows[0], *self.rows[:-1]])

    def test_missing_or_reordered_rpc_is_rejected(self):
        for records in (self.records[:-1], [self.records[1], self.records[0], *self.records[2:]]):
            with self.subTest(length=len(records)), self.assertRaises(ValueError):
                self.check(records=records)

    def test_wrong_trial_rpc_slice_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["rpc_sequences"] = []
        with self.assertRaisesRegex(ValueError, "Trial RPC sequence"):
            self.check(rows=rows)

    def test_changed_formal_settings_are_rejected(self):
        run = dict(self.metadata, smoke=True)
        with self.assertRaisesRegex(ValueError, "formal run settings"):
            self.check(run=run)

    def test_retry_attempt_gap_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["steps"][0]["attempt"] = 2
        with self.assertRaisesRegex(ValueError, "attempt sequence"):
            self.check(rows=rows)


class EvidenceTests(unittest.TestCase):
    def sample(self, case="transient-rebased"):
        rows = verify.archived(ROOT, "controlled", "observations")
        row = next(row for row in rows if row["profile"] == "sdk" and row["case_id"] == case and row["repetition"] == 1)
        records = [record for record in verify.archived(ROOT, "controlled", "rpc") if record["token"] == row["token"]]
        return row, records

    def replay(self, mutate=None, primary=False, case="transient-rebased"):
        row, records = self.sample(case)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(temporary)
            run = json.loads((ROOT / "data/retries/controlled/run.json").read_text())
            run.update(source_sha256={}, observations=1, profiles=["sdk"], case_ids=[case], repetitions=1)
            (folder / "manifest.json").write_text(json.dumps(run))
            (folder / "artifact-hashes.json").write_text("{}")
            destination = folder / "source/experiments/batch-retry/cases.json"
            destination.parent.mkdir(parents=True)
            destination.write_bytes((ROOT / "data/retries/fixtures/cases.json").read_bytes())
            def save():
                (folder / "observations.jsonl").write_text(json.dumps(row) + "\n")
                (folder / "rpc.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
            save()
            analyze.analyze(folder)
            if mutate:
                mutate(row, records)
                save()
            return analyze.analyze(folder) if primary else secondary.verify(folder)

    def test_both_decoders_accept_archived_retry_and_reconciliation(self):
        for case in ("transient-rebased", "short-details", "transport-partial", "transport-complete"):
            with self.subTest(case=case):
                self.assertTrue(self.replay(case=case)["passed"])

    def test_successful_original_operation_cannot_be_replayed(self):
        def mutate(row, records):
            row["steps"][1]["pending"][0]["original_index"] = 0
        for primary in (False, True):
            with self.subTest(primary=primary), self.assertRaises(AssertionError):
                self.replay(mutate, primary=primary)

    def test_retry_local_index_cannot_replace_original_index(self):
        def mutate(row, records):
            row["steps"][2]["pending"][0]["original_index"] = 1
        with self.assertRaises(AssertionError):
            self.replay(mutate)

    def test_changed_binary_request_is_rejected(self):
        row, records = self.sample()
        records[0]["request_binary"] = records[1]["request_binary"]
        with self.assertRaisesRegex(ValueError, "Binary request"):
            verify.verify_messages(ROOT, [row], records)

    def test_changed_final_binary_snapshot_is_rejected(self):
        def mutate(row, records):
            row["after"]["binary"] = []
        with self.assertRaises(AssertionError):
            self.replay(mutate)

    def test_unknown_outcome_requires_reconciliation_read(self):
        def mutate(row, records):
            del row["steps"][0]["application_read"]
        with self.assertRaises(AssertionError):
            self.replay(mutate, case="transport-partial")

    def test_changed_public_result_indexes_are_rejected(self):
        row, records = self.sample()
        row["steps"][0]["result"]["native_results"][1]["index"] = 0
        with self.assertRaisesRegex(ValueError, "Public status interpretation"):
            verify.verify_messages(ROOT, [row], records)


if __name__ == "__main__":
    unittest.main()
