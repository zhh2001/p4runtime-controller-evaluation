import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from finsy.proto import p4i
from google.protobuf import text_format
from fixtures import ROOT
from model import raw_entry,intent,initial_entries,key,semantics
from oracle import Invalid,validate


class TableOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info=text_format.Parse((ROOT/'fixtures/target.p4info.txt').read_text(),p4i.P4Info())
        cls.cases={c['id']:c for c in json.loads((ROOT/'fixtures/cases.json').read_text())}
        cls.state={key(e) for e in initial_entries(cls.info)}

    def test_all_frozen_intents(self):
        for c in self.cases.values():
            with self.subTest(case=c['id']):
                if c['category'] in ('invalid','policy'):
                    with self.assertRaises(Invalid):validate(self.info,raw_entry(self.info,c),c['update'],self.state)
                else:self.assertEqual(validate(self.info,raw_entry(self.info,c),c['update'],self.state),intent(self.info,c))

    def test_sparse_ids_are_not_positions(self):
        entry=raw_entry(self.info,self.cases['sparse-normal'])
        self.assertEqual([f.field_id for f in entry.match],[13,5])
        self.assertEqual([p.param_id for p in entry.action.action.params],[19,7])
        entry.action.action.params[0].param_id=1
        with self.assertRaises(Invalid):validate(self.info,entry,'MODIFY')

    def test_delete_ignores_nonsensical_nonkeys(self):
        c=self.cases['delete-ignored-action-ttl']
        self.assertEqual(validate(self.info,raw_entry(self.info,c),'DELETE'),intent(self.info,c))

    def test_constant_table_mutable_default(self):
        c=self.cases['constant-table-default-modify']
        self.assertEqual(validate(self.info,raw_entry(self.info,c),'MODIFY'),intent(self.info,c))

    def test_constant_default_cannot_be_reset(self):
        c=self.cases['constant-default-reset']
        with self.assertRaises(Invalid) as error:validate(self.info,raw_entry(self.info,c),'MODIFY')
        self.assertEqual(error.exception.code,7)

    def test_scope_is_different_from_membership(self):
        for name in ('scope-table_action-default','scope-default_action-ordinary'):
            with self.assertRaises(Invalid) as error:validate(self.info,raw_entry(self.info,self.cases[name]),'MODIFY')
            self.assertEqual(error.exception.code,7)

    def test_group_requires_selector(self):
        c=self.cases['indirect-group-1']
        with self.assertRaises(Invalid):validate(self.info,raw_entry(self.info,c),'MODIFY')
        c=self.cases['selected-group-1']
        self.assertEqual(validate(self.info,raw_entry(self.info,c),'MODIFY'),intent(self.info,c))

    def test_unknown_member_is_target_state(self):
        with self.assertRaises(Invalid) as error:validate(self.info,raw_entry(self.info,self.cases['indirect-member-99']),'MODIFY')
        self.assertEqual(error.exception.code,5)

    def test_default_normalization_changes_only_key_fields(self):
        for name in ('default-stale-match','default-stale-priority'):
            c=self.cases[name];entry=raw_entry(self.info,c);entry.ClearField('match');entry.priority=0
            self.assertEqual(validate(self.info,entry,'MODIFY'),intent(self.info,c))

    def test_full_state_contains_nine_defaults_and_entries(self):
        entries=initial_entries(self.info)
        self.assertEqual(len(entries),18);self.assertEqual(len({key(e) for e in entries}),18)
        self.assertEqual(sum(e.is_default_action for e in entries),9)

    def test_priorities_distinguish_entry_keys(self):
        e=raw_entry(self.info,self.cases['ternary-priority-10']);original=key(e);e.priority=11
        self.assertNotEqual(key(e),original)

    def test_negative_ternary_priority_is_missing_key_not_schema_error(self):
        entry=raw_entry(self.info,self.cases['ternary-priority--1'])
        self.assertEqual(validate(self.info,entry,'MODIFY')['priority'],-1)
        with self.assertRaises(Invalid) as error:validate(self.info,entry,'MODIFY',self.state)
        self.assertEqual(error.exception.code,5)

    def test_explicit_zero_ttl_and_reset_action(self):
        c=self.cases['default-reset'];result=validate(self.info,raw_entry(self.info,c),'MODIFY')
        self.assertEqual(result['ttl'],0);self.assertEqual(result['action']['params'][0][1],0)


if __name__=='__main__':unittest.main()
