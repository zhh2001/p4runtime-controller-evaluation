import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import run


class PersistenceTests(unittest.TestCase):
    def test_rejected_measurement_keeps_original_records(self):
        with tempfile.TemporaryDirectory(prefix="p4perf-validation-") as temporary:
            output=Path(temporary)/"run"
            instances=[]
            implementation=run.Driver
            def driver(*args,**kwargs):
                instance=implementation(*args,**kwargs)
                instances.append(instance)
                return instance
            arguments=["run.py","--output",str(output),"--oracle","--smoke",
                       "--profiles","raw-current","--repetitions","1","--warmup",".01","--seconds",".01"]
            with patch.object(run,"Driver",side_effect=driver),patch.object(sys,"argv",arguments),patch.object(run,"check_run",
                    side_effect=AssertionError("injected validation failure")):
                with self.assertRaisesRegex(AssertionError,"injected validation failure"):
                    run.main()
            failure=json.loads((output/"failure.json").read_text())
            raw=json.loads((output/failure["raw_file"]).read_text())
            self.assertEqual(raw["attempts"],4)
            self.assertEqual(len(raw["records"]),4)
            self.assertTrue(all(record["rpc_code"]==0 for record in raw["records"]))
            self.assertEqual(json.loads((output/"drivers.json").read_text())[0]["exit_code"],0)
            self.assertTrue(instances[0].process.stdin.closed)
            self.assertTrue(instances[0].process.stdout.closed)
            self.assertFalse(instances[0].thread.is_alive())


if __name__=="__main__":unittest.main()
