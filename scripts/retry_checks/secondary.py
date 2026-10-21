"""Independent stdlib verifier. No protobuf, retry-policy or analyzer imports."""

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path


def varint(data,offset):
    value=0
    for shift in range(0,70,7):
        if offset>=len(data):raise ValueError('truncated varint')
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if byte<128:return value,offset
    raise ValueError('oversized varint')


def fields(data):
    result={};offset=0
    while offset<len(data):
        tag,offset=varint(data,offset);number=tag>>3;kind=tag&7
        if not number:raise ValueError('zero field number')
        if kind==0:value,offset=varint(data,offset)
        elif kind==2:
            size,offset=varint(data,offset);end=offset+size
            if end>len(data):raise ValueError('truncated bytes')
            value=data[offset:end];offset=end
        elif kind in (1,5):
            size=8 if kind==1 else 4;value=data[offset:offset+size];offset+=size
            if len(value)!=size:raise ValueError('truncated fixed field')
        else:raise ValueError('unsupported wire type')
        result.setdefault(number,[]).append(value)
    return result


def one(data,number,default=None):
    values=data.get(number)
    if values is None:return default
    assert len(values)==1
    return values[0]


def b64(value):return base64.b64decode(value,validate=True)


def entity(data):
    entry=fields(one(fields(data),2))
    if one(entry,8,0) or 2 not in entry:return None
    assert len(entry[2])==1
    match=fields(entry[2][0]);exact=fields(one(match,2))
    result=dict(table_id=one(entry,1,0),field_id=one(match,1,0),key=int.from_bytes(one(exact,1,b''),'big'),
                metadata=one(entry,11,b'').decode())
    if 3 in entry:
        action=fields(one(fields(one(entry,3)),1));params=action[4];assert len(params)==1
        param=fields(params[0]);result.update(action_id=one(action,1,0),param_id=one(param,2,0),port=int.from_bytes(one(param,3,b''),'big'))
    return result


def json_entity(value):
    entry=value['table_entry']
    if entry.get('is_default_action') or not entry.get('match'):return None
    match=entry['match'][0]
    result=dict(table_id=entry['table_id'],field_id=match['field_id'],key=int.from_bytes(b64(match['exact']['value']),'big'),metadata=b64(entry.get('metadata','')).decode())
    if 'action' in entry:
        action=entry['action']['action'];param=action['params'][0]
        result.update(action_id=action['action_id'],param_id=param['param_id'],port=int.from_bytes(b64(param['value']),'big'))
    return result


def semantics(value):return {k:value[k] for k in ('key','port','metadata')}


def snapshot(value):
    decoded=[entity(b64(data)) for data in value['binary']]
    assert decoded==[json_entity(e) for e in value['entities']]
    return {v['key']:semantics(v) for v in decoded if v is not None}


def status_codes(record,count):
    if record['rpc_code']==0:return [0]*count
    if record['rpc_code']!=2 or 'raw_status' not in record:return None
    status=fields(b64(record['raw_status']))
    if one(status,1,0)!=2 or len(status.get(3,[]))!=count:return None
    codes=[]
    for raw in status.get(3,[]):
        any_value=fields(raw)
        if one(any_value,1,b'').decode().split('/')[-1]!='p4.v1.Error':return None
        code=one(fields(one(any_value,2,b'')),1,0)
        if code>16:return None
        codes.append(code)
    return codes if any(codes) else None


def expected_timeout(mode,case):
    return case in ({'transient-first','transient-rebased','all-transient','permanent-error','wrong-detail-type'} if mode=='controlled'
                    else {'duplicate-middle','mixed-conflicts','missing-delete'})


def verify(path):
    manifest=json.loads((path/'manifest.json').read_text());assert manifest['completed']
    for relative,digest in json.loads((path/'artifact-hashes.json').read_text()).items():assert hashlib.sha256((path/relative).read_bytes()).hexdigest()==digest,relative
    corpus=json.loads((path/'source/experiments/batch-retry/cases.json').read_text())[manifest['mode']]
    assert hashlib.sha256((path/'source/experiments/batch-retry/cases.json').read_bytes()).hexdigest()==manifest['plan']['cases_sha256']
    cases={c['id']:c for c in corpus}
    rows=[json.loads(line) for line in (path/'observations.jsonl').read_text().splitlines()]
    records=[json.loads(line) for line in (path/'rpc.jsonl').read_text().splitlines()]
    counts=Counter();seen=set();outcomes=Counter();case_results=[]
    for row in rows:
        identity=(row['repetition'],row['profile'],row['case_id']);assert identity not in seen;seen.add(identity)
        case=cases[row['case_id']];assert row['case']==case
        records_for_row=[r for r in records if r['token']==row['token']]
        assert row['rpc_sequences']==[r['sequence'] for r in records_for_row]
        writes=[r for r in records_for_row if r['kind']=='write'];reads=[r for r in records_for_row if r['kind']=='read']
        assert len(writes)==len(row['steps'])
        state=snapshot(row['before']);initial={s['key']:s for s in case['seeds']};assert state==initial
        successful=set();pending_indices=list(range(len(case['updates'])));read_count=0
        for attempt,(step,record) in enumerate(zip(row['steps'],writes)):
            request=fields(b64(record['request_binary']))
            assert one(request,1,0)==1 and one(request,5,0)==0
            assert one(fields(one(request,3)),2,0)==1
            binary_updates=request[4];json_updates=record['request']['updates']
            assert [v['original_index'] for v in step['pending']]==pending_indices,(identity,attempt)
            assert len(binary_updates)==len(json_updates)==len(pending_indices)
            actual_codes=[]
            for relative,(encoded,observed,op) in enumerate(zip(binary_updates,json_updates,step['pending'])):
                update=fields(encoded);kind=one(update,1,0);value=entity(one(update,2))
                assert kind=={'INSERT':1,'MODIFY':2,'DELETE':3}[observed['type']]
                assert value==json_entity(observed['entity'])
                index=op['original_index'];original=case['updates'][index]
                assert index not in successful,('success replayed',identity,index)
                assert value['key']==original['key']==op['key']
                assert kind=={'INSERT':1,'MODIFY':2,'DELETE':3}[op['type']]
                if kind!=3:assert semantics(value)=={k:original[k] for k in ('key','port','metadata')}
                if row['mode']=='controlled':code=case['schedule'][attempt][relative]
                else:code=6 if kind==1 and value['key'] in state else 5 if kind in (2,3) and value['key'] not in state else 0
                actual_codes.append(code)
                if code==0:
                    successful.add(index)
                    if kind==3:state.pop(value['key'],None)
                    else:state[value['key']]=semantics(value)
            assert state==snapshot(step['after'])
            counts['write_requests']+=1;counts['update_attempts']+=len(binary_updates)
            counts['successful_updates']+=actual_codes.count(0);counts['failed_updates']+=sum(v!=0 for v in actual_codes)
            result=step['result']
            if result.get('watchdog_timeout'):
                assert row['profile']=='shell' and attempt==len(row['steps'])-1
                assert row['outcome']=='watchdog_timeout';break
            assert record['rpc_code']==result['rpc_code']
            valid=status_codes(record,len(binary_updates))
            assert step['codes']==valid
            if valid is not None:assert valid==actual_codes
            if row['profile']=='sdk' and result['rpc_code']:
                exposed=result['native_results']
                if valid is None:assert exposed is None
                else:assert exposed==[dict(index=i,code=c) for i,c in enumerate(valid)]
            if 'application_read' in step:
                read=reads[read_count];read_count+=1
                assert read['sequence']>record['sequence']
                if attempt+1<len(writes):assert read['sequence']<writes[attempt+1]['sequence']
                binary_entities=[entity(item) for response in read['response_binary'] for item in fields(b64(response)).get(1,[])]
                read_state={v['key']:semantics(v) for v in binary_entities if v is not None}
                assert read_state==state
                assert [json_entity(e) for e in step['application_read']['entities']]==binary_entities
                counts['application_reads']+=1
            must_read=valid is None or any(v in (5,6) for v in valid)
            assert ('application_read' in step)==must_read
            next_indices=[];blocked=[]
            for relative,op in enumerate(step['pending']):
                index=op['original_index'];code=valid[relative] if valid is not None else None
                if code==0:continue
                if code in (8,14):next_indices.append(index)
                elif code is None or code in (5,6):
                    current=state.get(op['key'])
                    desired={k:op[k] for k in ('key','port','metadata')}
                    if (op['type']=='DELETE' and current is not None) or (op['type']!='DELETE' and current!=desired):next_indices.append(index)
                else:blocked.append(index)
            decision=step['decision'];assert [v['original_index'] for v in decision['pending']]==next_indices
            assert decision['blocked']==blocked
            for op in decision['pending']:
                index=op['original_index'];relative=pending_indices.index(index);code=valid[relative] if valid is not None else None
                expected=step['pending'][relative]['type']
                if (code is None or code in (5,6)) and expected!='DELETE':expected='MODIFY' if op['key'] in state else 'INSERT'
                assert op['type']==expected
            pending_indices=next_indices
        assert read_count==len(reads)
        assert state==snapshot(row['after'])
        desired=dict(initial)
        for op in case['updates']:
            if op['type']=='DELETE':desired.pop(op['key'],None)
            else:desired[op['key']]={k:op[k] for k in ('key','port','metadata')}
        timeout=row['profile']=='shell' and expected_timeout(row['mode'],case['id'])
        expected='watchdog_timeout' if timeout else 'blocked' if case['id']=='permanent-error' else 'completed'
        assert row['outcome']==expected,(identity,row['outcome'],expected)
        assert row['driver_exit']==(-9 if timeout else 0)
        if expected=='completed':assert state==desired
        if case['id']=='transport-complete':assert len(writes)==len(reads)==1
        counts['trials']+=1;counts[expected]+=1;outcomes[row['profile']+':'+expected]+=1
        case_results.append(dict(token=row['token'],outcome=expected,writes=len(writes),reads=len(reads),successful_original_indices=sorted(successful)))
    expected_set={(r,p,c) for r in range(1,manifest['repetitions']+1) for p in manifest['profiles'] for c in manifest['case_ids']}
    assert seen==expected_set
    assert len(records)==counts['write_requests']+counts['application_reads']
    analysis=json.loads((path/'analysis.json').read_text())
    assert all(analysis['counts'].get(k)==v for k,v in counts.items())
    assert analysis['profile_outcomes']==dict(outcomes)
    result=dict(passed=True,method='Python standard library only. Independent protobuf wire decoding and retry/state reconstruction.',
                counts=dict(counts),profile_outcomes=dict(outcomes),trials=case_results)
    (path/'independent-verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(path=str(path),counts=dict(counts))))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('paths',nargs='+',type=Path)
    for path in parser.parse_args().paths:verify(path)
