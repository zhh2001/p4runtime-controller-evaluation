import ast
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_inputs as verify
from input_checks import baseline


def archived(group, mode, filename):
    path = ROOT / "data/inputs" / group / mode / filename
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]


class InventoryTests(unittest.TestCase):
    def test_selected_inventory_and_source_functions(self):
        manifest = verify.verify_inventory(ROOT)
        self.assertEqual(len(manifest["files"]), 45)
        self.assertEqual(sum(run["observations"] for run in manifest["runs"]), 6984)
        provenance = json.loads((ROOT / "config/input-source-provenance.json").read_text())
        for filename, entry in provenance["files"].items():
            source = (ROOT / filename).read_text()
            functions = {node.name: ast.get_source_segment(source, node)
                         for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef)}
            for name, expected in entry.get("unchanged_functions_sha256", {}).items():
                self.assertEqual(hashlib.sha256(functions[name].encode()).hexdigest(), expected, (filename, name))

    def test_missing_inventory_item_and_changed_compressed_record_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "data/inputs", root / "data/inputs")
            manifest = root / "data/inputs/manifest.json"
            original = manifest.read_bytes()
            record = json.loads(original)
            record["files"].pop("data/inputs/baseline/controlled/observations.jsonl.gz")
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "file inventory"):
                verify.verify_inventory(root)
            manifest.write_bytes(original)
            path = root / "data/inputs/baseline/controlled/observations.jsonl.gz"
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify.verify_inventory(root)

    def test_wrong_uncompressed_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "data/inputs", root / "data/inputs")
            path = root / "data/inputs/manifest.json"
            record = json.loads(path.read_text())
            record["files"]["data/inputs/baseline/controlled/observations.jsonl.gz"]["uncompressed_sha256"] = "0" * 64
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "Uncompressed record differs"):
                verify.verify_inventory(root)


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observations = archived("boundaries", "controlled", "observations.jsonl.gz")
        cls.events = archived("boundaries", "controlled", "rpc.jsonl.gz")
        cls.corpus = json.loads((ROOT / "data/inputs/boundaries/fixtures/cases.json").read_text())
        cls.run_metadata = json.loads((ROOT / "data/inputs/boundaries/controlled/run.json").read_text())

    def check(self, observations, events=None):
        verify.verify_coverage(observations, self.events if events is None else events, self.corpus, self.run_metadata)

    def test_complete_coverage(self):
        self.check(self.observations)

    def test_missing_and_duplicate_observations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing, duplicate"):
            self.check(self.observations[:-1])
        with self.assertRaisesRegex(ValueError, "Missing, duplicate"):
            self.check([*self.observations[:-1], self.observations[0]])

    def test_embedded_case_change_is_rejected(self):
        observations = copy.deepcopy(self.observations)
        observations[0]["case"]["width"] = 2
        with self.assertRaisesRegex(ValueError, "Embedded source input"):
            self.check(observations)

    def test_unmatched_request_is_rejected(self):
        events = copy.deepcopy(self.events)
        events[0]["token"] = "other"
        with self.assertRaisesRegex(ValueError, "Unmatched or duplicate RPC"):
            self.check(self.observations, events)

    def test_wrong_device_id_is_rejected(self):
        events = copy.deepcopy(self.events)
        events[0]["request"]["device_id"] = "2"
        with self.assertRaisesRegex(ValueError, "device ID"):
            self.check(self.observations, events)


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads((ROOT / "data/inputs/baseline/fixtures/cases.json").read_text())
        cls.info = {mode: text_format.Parse((ROOT / "data/inputs/baseline/fixtures" /
                                            (mode + ".p4info.txt")).read_text(), p4i.P4Info())
                    for mode in ("controlled", "bmv2")}

    def sample(self, mode):
        observations = [row for row in archived("baseline", mode, "observations.jsonl.gz")
                        if row["adapter"] == "sdk" and row["case_id"] == "exact-zero"]
        events = [row for row in archived("baseline", mode, "rpc.jsonl.gz")
                  if baseline.event_identity(row, mode) in (("sdk@1", "exact-zero"), ("sdk@2", "exact-zero"))]
        return observations, events

    def test_valid_zero_requests_and_readbacks(self):
        for mode in ("controlled", "bmv2"):
            observations, events = self.sample(mode)
            result = baseline.verify(self.info[mode], self.corpus, observations, events, mode)
            self.assertEqual(result[2]["observations"], 2)

    def test_changed_accepted_parameter_is_rejected(self):
        observations, events = self.sample("controlled")
        for row in observations:
            row["wire"]["action"]["action"]["params"][0]["value"] = "Ag=="
        for event in events:
            event["request"]["updates"][0]["entity"]["table_entry"]["action"]["action"]["params"][0]["value"] = "Ag=="
        with self.assertRaises(AssertionError):
            baseline.verify(self.info["controlled"], self.corpus, observations, events, "controlled")

    def test_changed_readback_is_rejected(self):
        observations, events = self.sample("bmv2")
        observations[0]["after"][0]["action"]["action"]["params"][0]["value"] = "Ag=="
        with self.assertRaises(AssertionError):
            baseline.verify(self.info["bmv2"], self.corpus, observations, events, "bmv2")

    def test_duplicate_readback_field_is_rejected(self):
        observations, events = self.sample("bmv2")
        entry = observations[0]["after"][0]
        entry["match"].append(copy.deepcopy(entry["match"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate match field"):
            baseline.verify(self.info["bmv2"], self.corpus, observations, events, "bmv2")

    def test_explicit_range_wildcard_is_normalized_without_editing_input(self):
        table = next(t for t in self.info["bmv2"].tables
                     if any(f.match_type == p4i.MatchField.RANGE for f in t.match_fields))
        field = next(f for f in table.match_fields if f.match_type == p4i.MatchField.RANGE)
        entry = dict(table_id=table.preamble.id,
                     match=[dict(field_id=field.id, range=dict(low="AA==", high=base64.b64encode(b"\x01\xff").decode()))])
        saved = copy.deepcopy(entry)
        normalized, changes = baseline.normalize_read(entry, self.info["bmv2"])
        self.assertEqual(normalized["match"], [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(entry, saved)

    def test_optional_and_exact_zero_remain_matches(self):
        for kind in ("optional", "exact"):
            table = next(t for t in self.info["bmv2"].tables
                         if any(f.match_type == getattr(p4i.MatchField, kind.upper()) for f in t.match_fields))
            field = next(f for f in table.match_fields if f.match_type == getattr(p4i.MatchField, kind.upper()))
            entry = dict(table_id=table.preamble.id, match=[{"field_id": field.id, kind: {"value": "AA=="}}])
            normalized, changes = baseline.normalize_read(entry, self.info["bmv2"])
            self.assertEqual(normalized, entry)
            self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
