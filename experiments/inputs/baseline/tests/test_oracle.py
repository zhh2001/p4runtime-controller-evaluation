from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from google.protobuf import text_format, json_format
from finsy.proto import p4i, p4r
from server import number, validate_entry
from run import semantic_match, compare_failures

ROOT = Path(__file__).resolve().parents[1]


class OracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = text_format.Parse((ROOT/"fixtures/table_keys.p4info.txt").read_text(),p4i.P4Info())
        cls.tables = {t.preamble.name:t for t in cls.info.tables}
        cls.forward = next(a for a in cls.info.actions if a.preamble.name=="MyIngress.forward")

    def valid(self,table="t_exact",kind="exact",value=b"\x01",**fields):
        definition = self.tables["MyIngress."+table]
        action = p4r.Action(action_id=self.forward.preamble.id,params=[p4r.Action.Param(param_id=self.forward.params[0].id,value=b"\x01")])
        match = p4r.FieldMatch(field_id=definition.match_fields[0].id)
        getattr(match,kind).CopyFrom(getattr(p4r.FieldMatch, {"exact":"Exact","lpm":"LPM","ternary":"Ternary","range":"Range","optional":"Optional"}[kind])(**({"low":value} if kind=="range" else {"value":value}),**fields))
        return p4r.TableEntry(table_id=definition.preamble.id,match=[match],action=p4r.TableAction(action=action),priority=1 if kind in ("ternary","range","optional") else 0)

    def test_numeric_boundaries(self):
        self.assertEqual(number(b"\x01\xff",9),511)
        self.assertEqual(number(b"\0\0",9),0)
        for value in (b"",b"\x02\0"):
            with self.assertRaises(ValueError): number(value,9)

    def test_hand_checked_valid_entries(self):
        for entry in (self.valid(value=b"\0"),self.valid(value=b"\x01\xff"),self.valid("t_lpm","lpm",b"\x0a\x01\x02\0",prefix_len=24),self.valid("t_ternary","ternary",b"\x0f",mask=b"\x0f"),self.valid("t_range","range",b"\x01",high=b"\x0a"),self.valid("t_optional","optional",b"\0")):
            validate_entry(self.info,entry)

    def test_invalid_entries(self):
        overflow=self.valid(value=b"\x02\0")
        missing=self.valid();del missing.match[:]
        param=self.valid();param.action.action.params[0].value=b"\x02\0"
        unknown=self.valid();unknown.table_id=0
        missing_param=self.valid();del missing_param.action.action.params[:]
        priority=self.valid();priority.priority=1
        for entry in (overflow,missing,param,unknown,missing_param,priority,self.valid("t_lpm","lpm",b"\x0a\x01\x02\x03",prefix_len=24),self.valid("t_lpm","lpm",b"\x01",prefix_len=33),self.valid("t_ternary","ternary",b"\0",mask=b"\0"),self.valid("t_range","range",b"\x0a",high=b"\x01")):
            with self.assertRaises(ValueError): validate_entry(self.info,entry)

    def test_wildcard_omission(self):
        for table in ("t_lpm","t_ternary","t_range","t_optional"):
            entry=self.valid()
            entry.table_id=self.tables["MyIngress."+table].preamble.id
            del entry.match[:]
            entry.priority=0 if table=="t_lpm" else 1
            validate_entry(self.info,entry)

    def test_semantic_checker_detects_wrong_valid_values(self):
        case={"table":"MyIngress.t_exact","matches":[{"field":"s.ingress_port","kind":"exact","value":"01"}],"action":"MyIngress.forward","params":{"port":"01"},"priority":0}
        entry=self.valid()
        def wire():
            return json_format.MessageToDict(p4r.WriteRequest(updates=[p4r.Update(entity=p4r.Entity(table_entry=entry))]),preserving_proto_field_name=True)
        self.assertTrue(semantic_match(case,wire(),self.info))
        entry.action.action.params[0].value=b"\x02"
        self.assertFalse(semantic_match(case,wire(),self.info))
        entry.action.action.params[0].value=b"\x01"
        entry.match[0].exact.value=b"\x02"
        self.assertFalse(semantic_match(case,wire(),self.info))

    def test_failed_indexes_are_not_renumbered(self):
        codes=[0,6,5]
        self.assertTrue(compare_failures(codes,[{"index":0,"code":0},{"index":1,"code":6},{"index":2,"code":5}]))
        self.assertTrue(compare_failures(codes,[{"index":1,"code":6},{"index":2,"code":5}]))
        self.assertFalse(compare_failures(codes,[{"index":0,"code":6},{"index":1,"code":5}]))
        self.assertIsNone(compare_failures(codes,None))


if __name__=="__main__": unittest.main()
