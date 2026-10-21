import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2] / "data/sessions"
sys.path.insert(0, str(ROOT.parents[1] / "scripts"))
from session_checks import analyze


class RecoveryEvidenceTests(unittest.TestCase):
    def sample(self):
        data=json.loads((ROOT/'fixtures/recovery-validation-sample.json').read_text())
        return data['row'],{e['sequence']:e for e in data['events']}

    def test_real_ungated_recovery(self):
        row,events=self.sample();analyze.recovery(row,events)

    def test_old_state_is_not_restoration(self):
        row,events=self.sample();row['final_entries']=copy.deepcopy(row['initial_entries'])
        with self.assertRaises(AssertionError):analyze.recovery(row,events)

    def test_changed_duration_is_rejected(self):
        row,events=self.sample();row['fault_to_arbitration_ns']+=1
        with self.assertRaises(AssertionError):analyze.recovery(row,events)

    def test_unrequested_write_is_rejected(self):
        row,events=self.sample();row['writes'].append(copy.deepcopy(row['writes'][-1]))
        with self.assertRaises(AssertionError):analyze.recovery(row,events)

    def test_gate_event_is_rejected(self):
        row,events=self.sample();events[row['fault_sequence']]['event']='arbitration_gate_released'
        with self.assertRaises(AssertionError):analyze.recovery(row,events)

    def test_censored_failure_remains_a_record(self):
        row,events=self.sample();row.update(recovered=False,censored=True,censor_ns=60000000000)
        analyze.recovery(row,events)


if __name__=='__main__':unittest.main()
