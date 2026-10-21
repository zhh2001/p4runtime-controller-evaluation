import json
from pathlib import Path
import sys
import unittest

from finsy.proto import p4i
from google.protobuf import text_format

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
ROOT = REPOSITORY / "data/inputs/boundaries"
from input_checks.boundaries.model import raw_entity, intent
from input_checks.boundaries.oracle import Invalid, validate


class OracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = text_format.Parse((ROOT/"fixtures/controlled.p4info.txt").read_text(), p4i.P4Info())
        cls.cases = json.loads((ROOT/"fixtures/cases.json").read_text())

    def case(self, name):
        return next(c for c in self.cases if c["id"] == name)

    def test_complete_corpus_against_numeric_intent(self):
        self.assertEqual(len(self.cases), 286)
        for case in self.cases:
            with self.subTest(case=case["id"]):
                entity = raw_entity(self.info, case)
                if case["category"] == "invalid":
                    with self.assertRaises(Invalid): validate(self.info, entity, case["op"])
                else:
                    self.assertEqual(validate(self.info, entity, case["op"]), intent(self.info, case))

    def test_truncated_129_bit_value_is_detected(self):
        case = self.case("match-129-max")
        entity = raw_entity(self.info, case)
        entity.table_entry.match[0].exact.value = b"\xff"*16
        self.assertNotEqual(validate(self.info,entity,"write"),intent(self.info,case))

    def test_sign_extension_for_nine_bit_minimum(self):
        case=self.case("signed-9-sign-extended")
        self.assertEqual(validate(self.info,raw_entity(self.info,case),"write")["data"],-256)

    def test_positive_unsigned_bytes_are_not_signed(self):
        case=self.case("uint-8-max")
        self.assertEqual(validate(self.info,raw_entity(self.info,case),"write")["data"],255)

    def test_translation_is_not_array_position(self):
        case=self.case("index-counter-translated-write-10000")
        self.assertEqual(validate(self.info,raw_entity(self.info,case),"write")["index"],10000)
        case=self.case("index-counter-array-write-8")
        with self.assertRaises(Invalid):validate(self.info,raw_entity(self.info,case),"write")

    def test_read_all_omits_index(self):
        case=self.case("index-register-array-read--1")
        entity=raw_entity(self.info,case)
        self.assertFalse(entity.register_entry.HasField("index"))
        self.assertEqual(validate(self.info,entity,"read")["index"],"all")

    def test_false_tuple_member_is_not_missing(self):
        case=self.case("typed-tuple-valid")
        entity=raw_entity(self.info,case)
        entity.register_entry.data.tuple.members[1].bool=False
        self.assertEqual(validate(self.info,entity,"write")["data"],[5,False])

    def test_compound_oneof_is_checked(self):
        case=self.case("typed-tuple-valid")
        entity=raw_entity(self.info,case)
        entity.register_entry.data.tuple.members[1].bitstring=b"\x01"
        with self.assertRaises(Invalid):validate(self.info,entity,"write")

    def test_dynamic_varbit_width_is_checked(self):
        case=self.case("typed-varbit-nine")
        entity=raw_entity(self.info,case)
        entity.register_entry.data.varbit.bitwidth=8
        with self.assertRaises(Invalid):validate(self.info,entity,"write")

    def test_empty_translated_string_is_not_integer_zero(self):
        case=self.case("typed-text-empty")
        self.assertEqual(validate(self.info,raw_entity(self.info,case),"write")["data"],dict(text_bytes=""))


if __name__=="__main__":unittest.main()
