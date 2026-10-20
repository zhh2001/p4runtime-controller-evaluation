import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rpc_analysis", ROOT / "scripts/recompute_rpc.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class StatisticsTests(unittest.TestCase):
    def test_interval_and_sample_deviation(self):
        result = analysis.summarize([0, 2], np.array([[0, 0], [0, 1], [1, 0], [1, 1]]))
        self.assertEqual(result["mean"], 1)
        self.assertAlmostEqual(result["sample_stddev"], 2 ** .5)
        self.assertAlmostEqual(result["mean_ci95_low"], .075)
        self.assertAlmostEqual(result["mean_ci95_high"], 1.925)

    def test_zero_mean_has_undefined_cv(self):
        result = analysis.summarize([0, 0], np.array([[0, 1], [1, 0]]))
        self.assertEqual(result["cv_percent"], "")
        self.assertEqual(result["mean_ci95_low"], 0)
        self.assertEqual(result["mean_ci95_high"], 0)
        saved = {key: str(value) for key, value in result.items()}
        saved["cv_percent"] = "0"
        with self.assertRaisesRegex(ValueError, "Undefined CV"):
            analysis.compare_summary(result, saved, "zero sample")

    def test_pairing_precedes_averaging(self):
        index = {(profile, "insert", rep): {"successful_rpcs_per_second": value}
                 for profile, values in (("a", [2, 2]), ("b", [1, 4]))
                 for rep, value in enumerate(values, 1)}
        values = analysis.paired_values(index, "a/b", "insert", 2)
        self.assertEqual(values, [2, .5])
        self.assertEqual(np.mean(values), 1.25)


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "data/rpc/manifest.json").read_text())
        cls.point = cls.manifest["points"][0]
        cls.path = ROOT / "data/rpc" / cls.point["id"] / "measurement-rounds.csv"

    def check_modified(self, change, error):
        rows = analysis.read_csv(self.path)
        change(rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rounds.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, error):
                analysis.load_rounds(path, self.point, self.manifest)

    def test_missing_round_is_rejected(self):
        self.check_modified(lambda rows: rows.pop(), "Incomplete measurement")

    def test_duplicate_round_is_rejected(self):
        self.check_modified(lambda rows: rows.append(rows[0].copy()), "Duplicate measurement")

    def test_nonfinite_measurement_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(p99_ns="nan"), "Invalid measurement")

    def test_mixed_point_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(n="1"), "Incorrect point")

    def test_inconsistent_throughput_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(successful_rpcs_per_second="1"),
                            "Inconsistent throughput")

    def test_changed_file_is_rejected(self):
        manifest = json.loads(json.dumps(self.manifest))
        first = next(iter(manifest["files"].values()))
        first["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            analysis.verify_files(ROOT, manifest)


if __name__ == "__main__":
    unittest.main()
