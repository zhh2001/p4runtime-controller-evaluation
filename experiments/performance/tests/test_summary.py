from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from summarize import bootstrap_indices, linear_quantile, metrics, summarize


class SummaryTests(unittest.TestCase):
    def test_latency_and_resource_units(self):
        def sample(timestamp, user, system, rss):
            return dict(monotonic_ns=timestamp, user_ticks=user, system_ticks=system, rss_kib=rss)

        row = {
            "elapsed_ns": 2_000_000_000, "ok_updates": 16, "failed_calls": 0,
            "unknown_updates": 0,
            "records": [
                dict(start_ns=0, build_end_ns=100, end_ns=1000, rpc_code=0, read_count=0),
                dict(start_ns=1000, build_end_ns=1300, end_ns=4000, rpc_code=0, read_count=0),
            ],
            "resources": {
                "clock_ticks_per_second": 100,
                "before": {name: sample(0, 10, 20, 100) for name in ("client", "target")},
                "after": {name: sample(2_000_000_000, 210, 120, 200)
                          for name in ("client", "target")},
                "rss_samples": [{name: sample(1_000_000_000, 100, 60, 300)
                                 for name in ("client", "target")}],
            },
        }
        result = metrics(row)
        self.assertEqual(result["successful_rpcs_per_second"], 1)
        self.assertEqual(result["confirmed_updates_per_second"], 8)
        self.assertEqual(result["build_mean_ns"], 200)
        self.assertEqual(result["call_mean_ns"], 1800)
        self.assertEqual(result["p50_ns"], 1000)
        self.assertEqual(result["p99_ns"], 3000)
        self.assertEqual(result["call_p99_ns"], 2700)
        for name in ("client", "target"):
            self.assertEqual(result[name + "_cpu_core_percent"], 150)
            self.assertEqual(result[name + "_peak_rss_kib"], 300)
            self.assertEqual(result[name + "_observation_seconds"], 2)

    def test_zero_mean_and_interpolated_interval(self):
        indices = [[0, 0], [0, 1], [1, 0], [1, 1]]
        result = summarize([0, 0], indices)
        self.assertIsNone(result["cv_percent"])
        self.assertEqual(result["mean_ci95_low"], 0)
        self.assertEqual(linear_quantile([1, 2, 3, 4], .025), 1.075)
        result = summarize([2, 4], indices)
        self.assertEqual(result["mean"], 3)
        self.assertEqual(result["mean_ci95_low"], 2.075)
        self.assertAlmostEqual(result["mean_ci95_high"], 3.925)

    def test_bootstrap_is_repeatable(self):
        self.assertEqual(bootstrap_indices(10, 20, 20261017),
                         bootstrap_indices(10, 20, 20261017))
        self.assertNotEqual(bootstrap_indices(10, 20, 20261017),
                            bootstrap_indices(10, 20, 20261018))


if __name__ == "__main__":
    unittest.main()
