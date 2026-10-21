"""Reconstruct every update outcome without consulting application decisions."""

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path

from finsy.proto import p4r,rpc_status
from google.protobuf import json_format

from .model import semantic,state_map


def decode(entry):return semantic(json_format.MessageToDict(entry,preserving_proto_field_name=True))


def apply(state,update,code):
    if code:return
    key=int.from_bytes(update.entity.table_entry.match[0].exact.value,'big')
    if update.type==p4r.Update.DELETE:state.pop(key,None)
    else:state[key]=decode(update.entity)


def analyze(path):
    manifest=json.loads((path/'manifest.json').read_text());assert manifest['completed']
    assert manifest['observations']==len(manifest['profiles'])*len(manifest['case_ids'])*manifest['repetitions']
    for name,digest in manifest['source_sha256'].items():assert hashlib.sha256((path/'source'/name).read_bytes()).hexdigest()==digest,name
    for name,digest in json.loads((path/'artifact-hashes.json').read_text()).items():assert hashlib.sha256((path/name).read_bytes()).hexdigest()==digest,name
    observations=[json.loads(line) for line in (path/'observations.jsonl').read_text().splitlines()]
    rpc=[json.loads(line) for line in (path/'rpc.jsonl').read_text().splitlines()]
    counts=Counter();profile_outcomes={};details=[]
    for row in observations:
        case=row['case'];state=state_map(row['before']['entities']);succeeded=set();attempted=Counter()
        trace=[r for r in rpc if r['token']==row['token']];writes=[r for r in trace if r['kind']=='write'];reads=[r for r in trace if r['kind']=='read']
        assert len(writes)==len(row['steps'])
        for position,(step,record) in enumerate(zip(row['steps'],writes)):
            request=p4r.WriteRequest.FromString(base64.b64decode(record['request_binary']))
            assert request.atomicity==0 and request.device_id==1 and request.election_id.low==1
            pending=step['pending'];assert len(request.updates)==len(pending)
            before=dict(state);codes=[]
            for i,(op,update) in enumerate(zip(pending,request.updates)):
                original=op['original_index'];assert original not in succeeded,('successful item replayed',row['token'],original)
                assert op['key']==case['updates'][original]['key']
                assert int.from_bytes(update.entity.table_entry.match[0].exact.value,'big')==op['key']
                assert p4r.Update.Type.Name(update.type)==op['type']
                if row['mode']=='controlled':code=case['schedule'][position][i]
                else:
                    exists=op['key'] in before
                    code=6 if update.type==p4r.Update.INSERT and exists else 5 if update.type in (p4r.Update.MODIFY,p4r.Update.DELETE) and not exists else 0
                codes.append(code);apply(state,update,code);attempted[original]+=1
                if code==0:succeeded.add(original)
            assert state==state_map(step['after']['entities']),(row['token'],position,state,step['after'])
            if record['rpc_code']==0:assert not any(codes)
            elif case.get('response')=='normal' or position>0 or row['mode']=='bmv2':
                status=rpc_status.Status.FromString(base64.b64decode(record['raw_status']))
                actual=[]
                for detail in status.details:
                    error=p4r.Error();assert detail.Unpack(error);actual.append(error.canonical_code)
                assert actual==codes,(row['token'],actual,codes)
            result=step['result']
            if not result.get('watchdog_timeout'):
                assert result['rpc_code']==record['rpc_code']
                if step['codes'] is not None:assert step['codes']==codes
                if 'application_read' in step:
                    assert state_map(step['application_read']['entities'])==state
                    counts['application_reads']+=1
            counts['write_requests']+=1;counts['update_attempts']+=len(request.updates);counts['successful_updates']+=sum(c==0 for c in codes)
            counts['failed_updates']+=sum(c!=0 for c in codes)
        assert len(reads)==sum('application_read' in s for s in row['steps'])
        assert state==state_map(row['after']['entities'])
        desired={seed['key']:seed for seed in case['seeds']}
        for op in case['updates']:
            if op['type']=='DELETE':desired.pop(op['key'],None)
            else:desired[op['key']]={k:op[k] for k in ('key','port','metadata')}
        if row['outcome']=='completed':assert state==desired,(row['token'],state,desired)
        if row['outcome']=='blocked':assert row['blocked']==[1] and case['id']=='permanent-error' and len(writes)==1
        if row['outcome']=='watchdog_timeout':
            assert row['profile']=='shell' and row['driver_exit']==-9
        else:assert row['driver_exit']==0
        if case['id']=='transient-rebased' and row['profile']!='shell':
            assert [[op['original_index'] for op in s['pending']] for s in row['steps']]==[[0,1,2,3,4],[1,3],[3]]
        if case['id']=='transport-complete':assert len(writes)==1 and len(reads)==1
        counts['trials']+=1;counts[row['outcome']]+=1
        key=row['profile']+':'+row['outcome'];profile_outcomes[key]=profile_outcomes.get(key,0)+1
        details.append(dict(token=row['token'],outcome=row['outcome'],writes=len(writes),reads=len(reads),attempts=dict(attempted),final_state_matches_desired=state==desired))
    assert len(observations)==manifest['observations']
    if manifest['mode']=='bmv2':
        history=json.loads((path/'target-processes.json').read_text());assert all('stop_ns' in p and 'exit_code' in p for p in history)
        counts['target_processes_stopped']=len(history)
    result=dict(passed=True,counts=dict(counts),profile_outcomes=profile_outcomes,trials=details)
    (path/'analysis.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(dict(path=str(path),counts=dict(counts))))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('paths',nargs='+',type=Path)
    for path in parser.parse_args().paths:analyze(path)
