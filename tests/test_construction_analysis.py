import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import recompute_construction as analysis


class StatisticsTests(unittest.TestCase):
    def test_sample_deviation_and_linear_bootstrap_interval(self):
        result = analysis.summarize([0, 2], [[0, 0], [0, 1], [1, 0], [1, 1]])
        self.assertEqual(result["mean"], 1)
        self.assertAlmostEqual(result["sample_stddev"], 2 ** .5)
        self.assertAlmostEqual(result["mean_ci95_low"], .075)
        self.assertAlmostEqual(result["mean_ci95_high"], 1.925)

    def test_reuse_ratios_are_paired_before_averaging(self):
        index = {("entry", "sdk", "exact", mode, "", repetition): {"ns_per_op": value}
                 for mode, values in (("reused", [2, 2]), ("new", [1, 4]))
                 for repetition, value in enumerate(values, 1)}
        self.assertEqual(analysis.paired_values(index, ("exact", "sdk", "reused", "sdk", "new"), 2),
                         [2, .5])

    def test_changed_summary_interval_is_rejected(self):
        result = analysis.summarize([0, 2], [[0, 0], [0, 1], [1, 0], [1, 1]])
        saved = {field: str(value) for field, value in result.items()}
        saved["mean_ci95_high"] = "2"
        with self.assertRaisesRegex(ValueError, "Summary statistic differs"):
            analysis.compare_summary(result, saved)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / "data/construction"
        self.schedule = json.loads((self.base / "run.json").read_text())["schedule"]

    def test_go_uses_elapsed_time_instead_of_rounded_display(self):
        result = analysis.parse_output((self.base / "stdout/002.txt").read_text(), self.schedule[1])
        self.assertEqual(result["ns_per_op"], 1100356980 / 555530)
        self.assertNotEqual(result["ns_per_op"], result["reported_ns_per_op"])

    def test_wrong_go_benchmark_is_rejected(self):
        text = (self.base / "stdout/002.txt").read_text().replace("/sdk/exact/reused", "/sdk/exact/new")
        with self.assertRaisesRegex(ValueError, "Go benchmark identity differs"):
            analysis.parse_output(text, self.schedule[1])

    def test_inconsistent_go_display_is_rejected(self):
        text = (self.base / "stdout/002.txt").read_text().replace("1981 ns/op", "2100 ns/op")
        with self.assertRaisesRegex(ValueError, "Rounded Go ns/op differs"):
            analysis.parse_output(text, self.schedule[1])

    def test_disabled_python_gc_is_rejected(self):
        result = json.loads((self.base / "stdout/001.txt").read_text())
        result["gc_enabled"] = False
        with self.assertRaisesRegex(ValueError, "Python GC was not enabled"):
            analysis.parse_output(json.dumps(result), self.schedule[0])


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / "data/construction"
        cls.conditions = analysis.conditions(json.loads((cls.base / "plan.json").read_text()))
        cls.schedule = json.loads((cls.base / "run.json").read_text())["schedule"]

    def check_modified(self, change, message):
        rows = analysis.read_csv(self.base / "measurement-rounds.csv")
        change(rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rounds.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, message):
                analysis.load_rounds(path, self.conditions, 10, self.schedule)

    def test_missing_round_is_rejected(self):
        self.check_modified(lambda rows: rows.pop(), "Incomplete measurement")

    def test_duplicate_round_is_rejected(self):
        self.check_modified(lambda rows: rows.append(rows[0].copy()), "Duplicate measurement")

    def test_raw_reuse_condition_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(profile="raw-current", mode="reused"),
                            "Unexpected condition")

    def test_nonfinite_measurement_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(ns_per_op="nan"), "Invalid measurement")

    def test_python_allocation_value_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(bytes_per_op="0"), "must be empty")

    def test_execution_order_change_is_rejected(self):
        self.check_modified(lambda rows: rows[0].update(sequence="2"), "order differs")

    def test_changed_source_file_is_rejected(self):
        manifest = json.loads((self.base / "manifest.json").read_text())
        next(iter(manifest["files"].values()))["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            analysis.verify_files(ROOT, manifest)

    def test_archived_run_recomputes_from_all_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            report = analysis.recompute(ROOT, Path(directory))
        self.assertTrue(report["passed"])
        self.assertEqual(report["output_measurements_verified"], 270)
        self.assertEqual(report["metric_summaries"] + report["paired_summaries"], 81)
        self.assertEqual(report["numeric_fields_checked"], 567)


if __name__ == "__main__":
    unittest.main()
