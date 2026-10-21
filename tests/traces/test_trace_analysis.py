import gzip
import hashlib
from pathlib import Path
import sys
import unittest

from finsy.proto import p4i, p4r
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_rpc_traces import TableDecoder, check_run, phase_metrics


class StateTests(unittest.TestCase):
    def setUp(self):
        info = text_format.Parse((ROOT / "data/rpc/traces/control.p4info.txt").read_text(), p4i.P4Info())
        self.decoder = TableDecoder(info)

    def entry(self, key, port):
        decoder = self.decoder
        return p4r.Entity(table_entry=p4r.TableEntry(
            table_id=decoder.table.preamble.id,
            match=[p4r.FieldMatch(field_id=decoder.table.match_fields[0].id,
                                 exact=p4r.FieldMatch.Exact(value=bytes([key])))],
            action=p4r.TableAction(action=p4r.Action(action_id=decoder.action.preamble.id,
                params=[p4r.Action.Param(param_id=decoder.action.params[0].id, value=bytes([port]))])),
            metadata=b"E2"))

    def record(self, entities):
        raw = p4r.ReadResponse(entities=entities).SerializeToString(deterministic=True)
        packed = gzip.compress(raw, mtime=0)
        return packed, dict(gzip_sha256=hashlib.sha256(packed).hexdigest(),
                            protobuf_sha256=hashlib.sha256(raw).hexdigest(),
                            state_matches=True, entries=len(entities))

    def test_complete_state_and_mismatched_port(self):
        packed, record = self.record([self.entry(1, 2), self.entry(2, 1)])
        self.decoder.state(packed, record, {1: 2, 2: 1})
        with self.assertRaisesRegex(ValueError, "Full table state differs"):
            self.decoder.state(packed, record, {1: 1, 2: 1})

    def test_duplicate_key_and_changed_hash(self):
        packed, record = self.record([self.entry(1, 1), self.entry(1, 1)])
        with self.assertRaisesRegex(ValueError, "Duplicate readback key"):
            self.decoder.state(packed, record, {1: 1})
        record["gzip_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "gzip hash differs"):
            self.decoder.state(packed, record, {1: 1})

    def test_entity_and_action_encoding(self):
        item = self.entry(1, 1)
        item.table_entry.action.action.params[0].value = b"\x02\x00"
        with self.assertRaisesRegex(ValueError, "declared width"):
            self.decoder.entry(item.table_entry)
        packed, record = self.record([p4r.Entity(counter_entry=p4r.CounterEntry(counter_id=1))])
        with self.assertRaisesRegex(ValueError, "Unexpected readback entity"):
            self.decoder.state(packed, record, {})


class MetricTests(unittest.TestCase):
    def row(self):
        def sample(time, user, rss):
            return dict(monotonic_ns=time, user_ticks=user, system_ticks=0, rss_kib=rss)
        return {
            "elapsed_ns": 2_000_000_000, "ok_updates": 16, "failed_calls": 0, "unknown_updates": 0,
            "records": [
                dict(start_ns=0, build_end_ns=100, end_ns=1000, rpc_code=0, read_count=0),
                dict(start_ns=1000, build_end_ns=1300, end_ns=4000, rpc_code=0, read_count=0),
            ],
            "resources": {
                "clock_ticks_per_second": 100,
                "before": {name: sample(0, 10, 100) for name in ("client", "target")},
                "after": {name: sample(2_000_000_000, 310, 200) for name in ("client", "target")},
                "rss_samples": [{name: sample(1_000_000_000, 200, 300)
                                 for name in ("client", "target")}],
            },
        }

    def test_nearest_rank_build_call_and_resource_metrics(self):
        result = phase_metrics(self.row())
        expected = dict(successful_rpcs_per_second=1, confirmed_updates_per_second=8,
                        p50_ns=1000, p99_ns=3000, build_mean_ns=200, call_mean_ns=1800,
                        client_cpu_core_percent=150, target_peak_rss_kib=300)
        for field, value in expected.items():
            self.assertEqual(result[field], value)

    def test_reversed_resource_counters_are_rejected(self):
        row = self.row()
        row["resources"]["after"]["client"]["user_ticks"] = 0
        with self.assertRaisesRegex(ValueError, "Invalid resource interval"):
            phase_metrics(row)

    def test_concurrent_read_all_collects_the_full_table_per_call(self):
        row = self.row()
        row.update(operation="read_all", n=2, batch=1, concurrency=2,
                   start_ns=0, end_ns=2_000_000_000, requested_seconds=0,
                   attempts=2, rpc_calls=2, successful_calls=2, ok_updates=0,
                   failed_updates=0)
        row["records"] = [
            dict(worker=worker, iteration=0, admission_ns=0, start_ns=0,
                 build_end_ns=100, end_ns=1000, call_made=True, rpc_code=0,
                 first=0, count=0, read_count=2, key_sum=3, port_sum=3,
                 ok_updates=0, failed_updates=0, unknown_updates=0)
            for worker in range(2)
        ]
        expected = {1: 1, 2: 2}
        state, peak = check_run(row, expected)
        self.assertEqual(state, expected)
        self.assertEqual(peak, 2)
        self.assertEqual(phase_metrics(row)["read_entities_per_second"], 2)
        row["records"][1].update(read_count=1, key_sum=2, port_sum=2)
        with self.assertRaises(AssertionError):
            check_run(row, expected)


if __name__ == "__main__":
    unittest.main()
