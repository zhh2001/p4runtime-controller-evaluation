import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2] / "data/sessions"
sys.path.insert(0, str(ROOT.parents[1] / "scripts"))
from session_checks import analyze


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = json.loads((ROOT/"fixtures/validation-samples.json").read_text())

    def sample(self, kind):
        data = copy.deepcopy(self.samples[kind])
        return data["row"], {event["sequence"]: event for event in data["events"]}

    def test_observed_valid_records(self):
        for kind in self.samples:
            with self.subTest(kind=kind):
                row, events = self.sample(kind)
                getattr(analyze, kind)(row, events)

    def test_unaccounted_packet_loss_is_rejected(self):
        row, events = self.sample("consumers")
        row["application_dropped"] = 0
        with self.assertRaises(AssertionError):
            analyze.consumers(row, events)

    def test_duplicate_packet_delivery_is_rejected(self):
        row, events = self.sample("consumers")
        row["final"]["arrived"].append(row["final"]["arrived"][-1])
        with self.assertRaises(AssertionError):
            analyze.consumers(row, events)

    def test_received_cancelled_packet_is_rejected(self):
        row, events = self.sample("cancellation")
        received = next(event for event in row["server_events"] if event["event"] == "packet_out_received")
        received["packet_id"] = 20000
        row["received_packet_ids"] = [20000]
        events[received["sequence"]] = received
        with self.assertRaises(AssertionError):
            analyze.cancellation(row, events)

    def test_absent_demotion_is_rejected(self):
        row, events = self.sample("mastership")
        row["backup"]["is_primary"] = True
        with self.assertRaises(AssertionError):
            analyze.mastership(row, events)

    def test_changed_table_state_is_rejected(self):
        row, events = self.sample("mastership")
        row["final_entries"] = []
        with self.assertRaises(AssertionError):
            analyze.mastership(row, events)

    def test_rss_peak_uses_all_original_samples(self):
        row, events = self.sample("consumers")
        original_peak = max(item["rss_kib"] for item in row["rss_samples"])
        row["peak_sampled_rss_kib"] = original_peak
        row["rss_samples"].append({"monotonic_ns":row["rss_samples"][-1]["monotonic_ns"]+1,
                                   "rss_kib":original_peak+128})
        analyze.consumers(row, events)
        self.assertEqual(analyze.sampled_peak(row), original_peak+128)
        self.assertEqual(row["peak_sampled_rss_kib"], original_peak)


if __name__ == "__main__":
    unittest.main()
