"""Classify outcomes and check full target state against request semantics."""

import argparse
from collections import Counter,defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

from finsy.proto import p4i,p4r
from google.protobuf import json_format,text_format

from .model import key,semantics,intent,default_entry
from .oracle import Invalid,validate



def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):return [json.loads(line) for line in path.read_text().splitlines()]


def snapshot(values):
    tables={};others=[]
    for value in values:
        entity=json_format.ParseDict(value,p4r.Entity())
        if entity.HasField('table_entry'):
            identity=key(entity.table_entry)
            assert identity not in tables,identity
            tables[identity]=semantics(entity.table_entry)
        else:others.append(json.dumps(value,sort_keys=True))
    return tables,sorted(others)


def analyze(output):
    manifest=json.loads((output/'manifest.json').read_text());assert manifest['completed']
    for name,expected in manifest['source_sha256'].items():assert digest(output/'source'/name)==expected,name
    info=text_format.Parse((output/'source/experiments/table-constraints/fixtures/target.p4info.txt').read_text(),p4i.P4Info())
    observations=rows(output/'observations.jsonl');events=rows(output/'rpc.jsonl');by_token=defaultdict(list)
    for event in events:
        assert event['token'] is not None
        by_token[event['token']].append(event)
    assert len(observations)==len(manifest['case_ids'])*len(manifest['profiles'])*manifest['repetitions']==manifest['observations']
    assert len({r['token'] for r in observations})==len(observations)
    result=[];counts=Counter();pairs=defaultdict(list);findings=[];state_checks=0
    for row in observations:
        assert 'Exception calling application:' not in row.get('error',''),row['token']
        case=row['case'];trace=by_token.get(row['token'],[])
        assert [e['sequence'] for e in trace]==row['rpc_sequences']
        assert len(trace)<=1
        before,others_before=snapshot(row['before']);after,others_after=snapshot(row['after'])
        expected=intent(info,case);wire=None;protocol_error=None;entry=None
        if row['stage']=='not_expressible':
            assert not trace;outcome='not_expressible'
        elif not trace:
            assert row.get('error');outcome='local_panic' if 'adapter recovered panic:' in row['error'] else 'local_rejected'
        else:
            event=trace[0];assert event['operation']=='write'
            request=json_format.ParseDict(event['request'],p4r.WriteRequest())
            assert request.device_id==1 and request.election_id.low==1 and len(request.updates)==1
            update=request.updates[0];assert p4r.Update.Type.Name(update.type)==case['update']
            assert update.entity.WhichOneof('entity')=='table_entry';entry=update.entity.table_entry
            try:wire=validate(info,entry,case['update'],before)
            except Invalid as error:protocol_error=dict(code=error.code,message=str(error))
            if event['rpc_code']:
                assert row.get('error');outcome='target_rejected'
            else:
                assert not row.get('error');outcome='accepted'
        flags=[]
        if case['category'] in ('valid','normalizable') and outcome not in ('accepted','not_expressible'):flags.append('valid_input_rejected')
        if outcome=='accepted':
            if protocol_error:flags.append('target_accepted_invalid_wire')
            if protocol_error is None and wire!=expected:flags.append('accepted_semantic_mismatch')
            if case['category']=='invalid':flags.append('invalid_input_accepted')
        if protocol_error and trace and trace[0]['rpc_code'] and trace[0].get('details')!=[protocol_error['code']]:
            flags.append('target_status_differs_from_oracle')
        if outcome=='local_panic':flags.append('library_panic')
        if manifest['mode']=='controlled':
            assert not (outcome=='accepted' and protocol_error)
            if outcome=='target_rejected':assert protocol_error is not None
        desired=before.copy()
        if outcome=='accepted':
            if case['update']=='DELETE':desired.pop(key(entry))
            else:
                value=semantics(entry)
                if entry.is_default_action and not entry.HasField('action'):
                    table=next(t for t in info.tables if t.preamble.id==entry.table_id)
                    value['action']=semantics(default_entry(info,table))['action']
                desired[key(entry)]=value
        assert others_before==others_after,row['token']
        state_matches=after==desired
        if not state_matches:flags.append('readback_differs_from_request')
        changed={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
        assert all(k[0]==expected['table'] for k in changed),row['token']
        if outcome!='accepted':assert state_matches,row['token']
        state_checks+=1
        evaluation=dict(profile=row['profile'],repetition=row['repetition'],case_id=case['id'],family=case['family'],category=case['category'],
                        outcome=outcome,rpc_observed=bool(trace),rpc_code=trace[0]['rpc_code'] if trace else '',
                        target_details=json.dumps(trace[0].get('details') or []) if trace else '',
                        semantics_match=wire==expected if outcome=='accepted' else '',state_matches_request=state_matches,
                        changed_table_entries=len(changed),flags='|'.join(flags))
        result.append(evaluation);counts[row['profile'],case['family'],case['category'],outcome]+=1
        pairs[row['profile'],case['id']].append((outcome,evaluation['rpc_code'],evaluation['semantics_match'],evaluation['flags']))
        if flags:findings.append(dict(token=row['token'],profile=row['profile'],case_id=case['id'],flags=flags,error=row.get('error'),protocol_error=protocol_error))
    assert all(len(v)==manifest['repetitions'] and len(set(v))==1 for v in pairs.values())
    assert all(r['code']==0 for r in manifest['driver_exits'])
    for path in output.rglob('*.stderr'):assert 'DATA RACE' not in path.read_text(),path
    count_rows=[dict(profile=k[0],family=k[1],category=k[2],outcome=k[3],count=v) for k,v in sorted(counts.items())]
    for name,values in (('case-results.csv',result),('counts.csv',count_rows)):
        with (output/name).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(values[0]));writer.writeheader();writer.writerows(values)
    (output/'findings.json').write_text(json.dumps(findings,indent=2)+'\n')
    verification=dict(verified_local=datetime.now().astimezone().isoformat(),observations=len(observations),tested_path_rpc_calls=len(events),
        full_state_checks=state_checks,repeat_classifications_identical=True,driver_processes_exited=len(manifest['driver_exits']),
        finding_observations=len(findings),finding_counts=dict(Counter(flag for f in findings for flag in f['flags'])),source_snapshot_verified=True,
        files_sha256={str(p.relative_to(output)):digest(p) for p in output.rglob('*') if p.is_file() and p.name!='verification.json'})
    (output/'verification.json').write_text(json.dumps(verification,indent=2)+'\n')
    print(json.dumps({k:v for k,v in verification.items() if k!='files_sha256'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();analyze(args.output.resolve())
