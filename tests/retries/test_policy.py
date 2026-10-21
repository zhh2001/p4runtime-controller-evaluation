import base64
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'scripts'))
from retry_checks.model import operation,retry_plan,reconcile
from retry_checks.policy import complete_codes
from finsy.proto import p4r,rpc_status
from google.protobuf import any_pb2


def status_row(codes,**extra):
    status=rpc_status.Status(code=2)
    for code in codes:
        detail=any_pb2.Any();detail.Pack(p4r.Error(canonical_code=code));status.details.append(detail)
    return dict(rpc_code=2,raw_status=base64.b64encode(status.SerializeToString()).decode(),**extra)


class RetryPolicy(unittest.TestCase):
    def test_original_indices_survive_second_subset(self):
        updates=[operation(i) for i in range(5)]
        first=retry_plan(updates,[0,14,0,8,0])['pending']
        self.assertEqual([v['original_index'] for v in first],[1,3])
        second=retry_plan(first,[0,14])['pending']
        self.assertEqual([v['original_index'] for v in second],[3])
        self.assertEqual(second[0],updates[3])

    def test_does_not_replay_success_or_permanent_failure(self):
        updates=[operation(i) for i in range(4)]
        result=retry_plan(updates,[0,3,14,0])
        self.assertEqual(result['blocked'],[1])
        self.assertEqual([v['original_index'] for v in result['pending']],[2])

    def test_conflicts_require_read(self):
        self.assertEqual(retry_plan([operation(0)],[6]),dict(read_required=True))
        self.assertEqual(retry_plan([operation(0)],None),dict(read_required=True))

    def test_conflicts_follow_desired_state(self):
        updates=[operation(0),operation(1,'MODIFY'),operation(2,'DELETE')]
        state={1:dict(key=1,port=1,metadata='seed')}
        result=retry_plan(updates,[6,5,5],state)
        self.assertEqual([(v['original_index'],v['type']) for v in result['pending']],[(0,'MODIFY'),(1,'INSERT')])

    def test_unknown_outcome_omits_already_applied(self):
        updates=[operation(i) for i in range(3)]
        state={v['key']:{k:v[k] for k in ('key','port','metadata')} for v in (updates[0],updates[2])}
        self.assertEqual(retry_plan(updates,None,state)['pending'],[updates[1]])

    def test_policy_does_not_mutate_inputs(self):
        updates=[operation(0),operation(1)];before=copy.deepcopy(updates)
        reconcile(updates,{1:dict(key=1,port=1,metadata='seed')})
        retry_plan(updates,[14,8]);self.assertEqual(updates,before)


class StatusGuard(unittest.TestCase):
    def test_sdk_uses_only_public_results(self):
        row=status_row([0,14,0],native_results=None)
        self.assertIsNone(complete_codes(row,3,'sdk')[0])
        row['native_results']=[dict(index=i,code=c) for i,c in enumerate([0,14,0])]
        self.assertEqual(complete_codes(row,3,'sdk')[0],[0,14,0])

    def test_native_failure_map(self):
        row=status_row([0,14,0,8],native_results=[dict(index=1,code=14),dict(index=3,code=8)])
        self.assertEqual(complete_codes(row,4,'finsy')[0],[0,14,0,8])
        row['native_results'][0]['index']=0
        self.assertIsNone(complete_codes(row,4,'finsy')[0])

    def test_short_status_is_not_a_complete_failure_map(self):
        row=status_row([14],native_results=[dict(index=0,code=14)])
        for profile in ('legacy','raw-current','raw-legacy','finsy','shell'):
            with self.subTest(profile=profile):self.assertIsNone(complete_codes(row,3,profile)[0])

    def test_wrong_type_rejected(self):
        status=rpc_status.Status(code=2)
        detail=any_pb2.Any();detail.Pack(p4r.TableEntry());status.details.append(detail)
        row=dict(rpc_code=2,raw_status=base64.b64encode(status.SerializeToString()).decode())
        self.assertIsNone(complete_codes(row,1,'legacy')[0])

    def test_missing_all_ok_and_out_of_range_rejected(self):
        for row in (dict(rpc_code=2),status_row([0]),status_row([17])):
            with self.subTest(row=row):self.assertIsNone(complete_codes(row,1,'legacy')[0])

    def test_transport_error_needs_read(self):
        row=status_row([14]);row['rpc_code']=14
        self.assertIsNone(complete_codes(row,1,'raw-current')[0])

    def test_success_infers_all_successful(self):
        self.assertEqual(complete_codes(dict(rpc_code=0),3,'sdk')[0],[0,0,0])


if __name__=='__main__':unittest.main()
