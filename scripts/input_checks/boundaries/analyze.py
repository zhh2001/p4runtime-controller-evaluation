"""Evaluate requests and state against independent input semantics."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

from finsy.proto import p4i, p4r
from google.protobuf import json_format, text_format

from .model import declaration, intent
from .oracle import Invalid, validate




def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(path):return [json.loads(line) for line in path.read_text().splitlines()]


def state_key(semantics):return semantics['kind'],semantics['id'],semantics.get('index',semantics.get('key'))


def public_count(row):
    values=row.get('public')
    if values is None:return 0
    if isinstance(values,dict):return 1
    return sum(len(v.get('entities',[])) for v in values) if row['profile']=='finsy' else len(values)


def analyze(output):
    manifest=json.loads((output/'manifest.json').read_text())
    assert manifest['completed']
    for name,expected in manifest['source_sha256'].items():assert digest(output/'source'/name)==expected,name
    info=text_format.Parse((output/'source/experiments/boundaries/fixtures/controlled.p4info.txt').read_text(),p4i.P4Info())
    observations=load_rows(output/'observations.jsonl')
    rpc=load_rows(output/'rpc.jsonl')
    by_token=defaultdict(list)
    for event in rpc:
        assert event['token'] is not None
        by_token[event['token']].append(event)
    assert len(observations)==len(manifest['case_ids'])*len(manifest['profiles'])*manifest['repetitions']==manifest['observations']
    assert len({r['token'] for r in observations})==len(observations)
    counts=Counter();state={};evaluations=[];accepted_mismatches=[];valid_rejections=[];invalid_acceptances=[]
    for row in observations:
        case=row['case'];events=by_token.get(row['token'],[])
        assert [e['sequence'] for e in events]==row['rpc_sequences']
        assert len(events)<=1,(row['token'],len(events))
        expected=intent(info,case)
        semantics=None
        if row['stage']=='not_expressible':
            assert not events
            outcome='not_expressible'
        elif not events:
            assert row.get('error')
            outcome='local_rejected'
        else:
            event=events[0]
            assert event['operation']==case['op']
            request=json_format.ParseDict(event['request'],p4r.ReadRequest() if case['op']=='read' else p4r.WriteRequest())
            entity=request.entities[0] if case['op']=='read' else request.updates[0].entity
            if event['rpc_code']:
                assert row.get('error'),row['token']
                outcome='target_rejected'
                if manifest['mode']=='controlled':
                    try:validate(info,entity,case['op'])
                    except Invalid:pass
                    else:raise AssertionError(('controlled endpoint rejected valid request',row['token']))
            else:
                try:semantics=validate(info,entity,case['op'])
                except Invalid:
                    assert manifest['mode']=='bmv2' and case['op']=='read',row['token']
                if case['op']=='read':
                    responses=[json_format.ParseDict(v,p4r.ReadResponse()) for v in event['responses']]
                    entities=[e for reply in responses for e in reply.entities]
                    if not entities and case['category']=='invalid':
                        outcome='public_error_after_empty_read' if row.get('error') else 'empty_read'
                    else:
                        assert not row.get('error'),row['token']
                        assert public_count(row)==len(entities),(row['token'],public_count(row),len(entities))
                        outcome='accepted'
                    if manifest['mode']=='controlled':
                        indexes=([10000,10001] if case.get('translated') else list(range(8))) if case['index']==-1 else [case['index']]
                        assert len(entities)==len(indexes)
                        observed={}
                        for entity in entities:
                            value=validate(info,entity,'write');observed[value['index']]=value
                            key=state_key(value)
                            if key in state:assert value==state[key],(row['token'],value,state[key])
                            else:
                                default=0 if case['kind']=='register' else dict(packets=0,bytes=0) if case['kind']=='counter' else dict(cir=0,pir=0,cburst=0,pburst=0)
                                assert value['data']==default
                        assert set(observed)==set(indexes)
                else:
                    assert not row.get('error'),row['token']
                    outcome='accepted'
                    state[state_key(semantics)]=semantics
                if outcome=='accepted' and semantics!=expected:accepted_mismatches.append(row['token'])
        if case['category']!='invalid' and outcome not in ('accepted','not_expressible'):valid_rejections.append(row['token'])
        if case['category']=='invalid' and outcome=='accepted':invalid_acceptances.append(row['token'])
        if manifest['mode']=='bmv2':
            if outcome!='accepted' or case['op']=='read':assert row['before']==row['after'],row['token']
            elif case['kind'] in ('match','param'):
                table_id=declaration(info,'table',case['table']).preamble.id
                matched=[]
                for value in row['after']:
                    entity=json_format.ParseDict(value,p4r.Entity())
                    if entity.WhichOneof('entity')=='table_entry' and entity.table_entry.table_id==table_id:
                        s=validate(info,entity,'write')
                        if s['key']==case['expected']['key']:matched.append(s)
                assert matched==[expected],(row['token'],matched,expected)
            else:
                matches=[validate(info,json_format.ParseDict(v,p4r.Entity()),'write') for v in row['after']
                         if case['kind']+'_entry' in v and v[case['kind']+'_entry'][case['kind']+'_id']==expected['id']
                         and int(v[case['kind']+'_entry'].get('index',{}).get('index',0))==case['index']]
                assert matches==[expected],(row['token'],matches,expected)
        counts[row['profile'],case['family'],case['category'],outcome]+=1
        evaluations.append(dict(profile=row['profile'],repetition=row['repetition'],case_id=case['id'],family=case['family'],category=case['category'],
                                outcome=outcome,rpc_observed=bool(events),rpc_code=events[0]['rpc_code'] if events else '',semantics_match=semantics==expected if outcome=='accepted' else ''))
    assert not accepted_mismatches and not invalid_acceptances,(accepted_mismatches,invalid_acceptances)
    if manifest['mode']=='controlled':assert not valid_rejections,valid_rejections
    assert all(r['code']==0 for r in manifest['driver_exits'])
    for path in output.rglob('*.stderr'):assert 'DATA RACE' not in path.read_text(),path
    counts_rows=[dict(profile=k[0],family=k[1],category=k[2],outcome=k[3],count=v) for k,v in sorted(counts.items())]
    for name,rows in (('case-results.csv',evaluations),('counts.csv',counts_rows)):
        with (output/name).open('w',newline='') as file:
            writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    pairs=defaultdict(list)
    for row in evaluations:pairs[row['profile'],row['case_id']].append((row['outcome'],row['rpc_code'],row['semantics_match']))
    assert all(len(values)==manifest['repetitions'] and len(set(values))==1 for values in pairs.values())
    result=dict(verified_local=datetime.now().astimezone().isoformat(),observations=len(observations),rpc_calls=len(rpc),
                unique_case_profile_pairs=len(pairs),repeat_classifications_identical=True,accepted_semantic_mismatches=0,
                invalid_write_acceptances=0,valid_rejections=valid_rejections,driver_processes_exited=len(manifest['driver_exits']),
                source_snapshot_verified=True,counts=counts_rows,
                files_sha256={str(p.relative_to(output)):digest(p) for p in output.rglob('*') if p.is_file() and p.name!='verification.json'})
    (output/'verification.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('files_sha256','counts','valid_rejections')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();analyze(args.output.resolve())
