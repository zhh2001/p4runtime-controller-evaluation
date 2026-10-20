import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from audit import check_run,percentile,statistics


class AccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples=json.loads((ROOT/"fixtures/audit-samples.json").read_text())
        cls.read_state={int(key):value for key,value in json.loads(
            (ROOT/"fixtures/audit-read-state.json").read_text()).items()}

    def test_finite_insert_and_failed_rpc(self):
        inserted,_=check_run(self.samples[0],{})
        self.assertEqual(inserted,{key:1 for key in range(1,65)})
        changed,_=check_run(self.samples[1],inserted,True)
        self.assertEqual(sum(port==1 for port in changed.values()),16)
        self.assertEqual(self.samples[1]["unknown_updates"],16)

    def test_counter_disagreement_is_rejected(self):
        row=copy.deepcopy(self.samples[0])
        row["ok_updates"]+=1
        with self.assertRaises(AssertionError):check_run(row,{})

    def test_missing_insert_batch_is_rejected(self):
        row=copy.deepcopy(self.samples[0])
        row["records"].pop()
        for field in ("attempts","rpc_calls","successful_calls"):row[field]-=1
        row["ok_updates"]-=16
        with self.assertRaises(AssertionError):check_run(row,{})

    def test_read_content_is_checked(self):
        row=copy.deepcopy(self.samples[2])
        state=self.read_state
        check_run(row,state)
        row["records"][0]["key_sum"]+=1
        with self.assertRaises(AssertionError):check_run(row,state)

    def test_per_worker_calls_are_sequential(self):
        row=copy.deepcopy(self.samples[2])
        check_run(row,self.read_state)
        self.assertGreater(len(row["records"]),1)
        row["records"][1]["admission_ns"]=row["records"][0]["end_ns"]-1
        with self.assertRaises(AssertionError):check_run(row,self.read_state)

    def test_percentiles_keep_failure_latency(self):
        row={"elapsed_ns":2_000_000_000,"ok_updates":4,"failed_calls":1,"unknown_updates":2,
             "records":[{"start_ns":0,"end_ns":delay,"rpc_code":code,"read_count":0}
                        for delay,code in [(100,0),(200,0),(10000,14)]]}
        result=statistics(row)
        self.assertEqual(result["p50_ns"],200)
        self.assertEqual(result["p99_ns"],10000)
        self.assertEqual(result["successful_rpcs_per_second"],1)
        self.assertEqual(result["confirmed_updates_per_second"],2)
        self.assertEqual(result["unknown_updates"],2)

    def test_nearest_rank(self):
        self.assertEqual(percentile([1,2,3,100],.5),2)
        self.assertEqual(percentile([1,2,3,100],.95),100)


if __name__=="__main__":unittest.main()
