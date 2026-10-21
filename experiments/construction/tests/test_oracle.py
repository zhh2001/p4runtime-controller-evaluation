import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from finsy.proto import p4r
from google.protobuf import json_format

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oracle import verify


class OracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Read a known successful SDK output from the current preflight.
        folder = Path(os.environ["CONSTRUCTION_PREFLIGHT"])
        records = json.loads((folder / "sdk-samples.json").read_text())
        cls.record = next(r for r in records if r["workload"] == "mixed" and r["mode"] == "new" and r["index"] == 0)

    def run_record(self, change=None, rejected=False):
        record = copy.deepcopy(self.record)
        message = json_format.ParseDict(record["wire"], p4r.TableEntry())
        if change:
            change(message)
        record["wire"] = json_format.MessageToDict(message, preserving_proto_field_name=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            path.write_text(json.dumps([record]))
            if rejected:
                with self.assertRaises((AssertionError, KeyError)):
                    verify(path)
            else:
                self.assertEqual(verify(path), 1)

    def test_optional_zero_is_present(self):
        self.run_record()

    def test_equal_padded_parameter_is_accepted(self):
        self.run_record(lambda m: setattr(m.action.action.params[0], "value", b"\x00\x01"))

    def test_different_parameter_is_rejected(self):
        self.run_record(lambda m: setattr(m.action.action.params[0], "value", b"\x02"), True)

    def test_parameter_outside_bitwidth_is_rejected(self):
        self.run_record(lambda m: setattr(m.action.action.params[0], "value", b"\x02\x01"), True)

    def test_absent_optional_zero_is_rejected(self):
        self.run_record(lambda m: m.match.pop(), True)

    def test_different_prefix_is_rejected(self):
        self.run_record(lambda m: setattr(m.match[1].lpm, "prefix_len", 15), True)

    def test_different_mask_is_rejected(self):
        self.run_record(lambda m: setattr(m.match[2].ternary, "mask", b"\xff\xff"), True)

    def test_range_bound_is_rejected(self):
        self.run_record(lambda m: setattr(m.match[3].range, "high", b"\x01"), True)

    def test_changed_metadata_is_rejected(self):
        self.run_record(lambda m: setattr(m, "metadata", b"wrong"), True)


if __name__ == "__main__":
    unittest.main()
