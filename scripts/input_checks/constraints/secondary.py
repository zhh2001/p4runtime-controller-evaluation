"""Independent table-state checks using JSON, base64 and integers."""

import base64
from collections import Counter, defaultdict
import csv
import hashlib
import json


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def number(value):return int.from_bytes(base64.b64decode(value),'big')


def action(value):
    if 'action' in value:
        a=value['action'];return ('direct',a['action_id'],tuple(sorted((p['param_id'],number(p['value'])) for p in a.get('params',[]))))
    if 'action_profile_member_id' in value:return ('member',value['action_profile_member_id'])
    if 'action_profile_group_id' in value:return ('group',value['action_profile_group_id'])
    assert not value,value
    return None


def entry(value):
    matches=[]
    for f in value.get('match',[]):
        if 'exact' in f:matches.append((f['field_id'],'exact',number(f['exact']['value'])))
        else:matches.append((f['field_id'],'ternary',number(f['ternary']['value']),number(f['ternary']['mask'])))
    identity=(value['table_id'],value.get('is_default_action',False),value.get('priority',0),tuple(sorted(matches)))
    return identity,(action(value.get('action',{})),int(value.get('idle_timeout_ns',0)))


def snapshot(values):
    tables={};other=[]
    for e in values:
        if 'table_entry' in e:
            k,v=entry(e['table_entry']);assert k not in tables;tables[k]=v
        else:other.append(json.dumps(e,sort_keys=True))
    return tables,sorted(other)


def expected(schema,c):
    table=schema['tables'][c['table']];matches=[]
    for f in c['matches']:
        if f['name'] not in table['fields']:continue
        value=(table['fields'][f['name']]['id'],f['kind'],int(f['value'],16))
        if f['kind']=='ternary':value+=(int(f['mask'],16),)
        matches.append(value)
    identity=(table['id'],c['default'],c['priority'],tuple(sorted(matches)))
    if c['category']=='policy':identity=(table['id'],True,0,())
    if c['action_kind']=='direct':
        a=schema['actions'][c['action']]
        av=('direct',a['id'],tuple(sorted((a['params'][p['name']]['id'],int(p['value'],16)) for p in c['params'] if p['name'] in a['params'])))
    elif c['action_kind'] in ('member','group'):av=(c['action_kind'],c['reference'])
    else:
        name='NoAction' if c['table'].endswith('.sparse') else 'MyIngress.common'
        a=schema['actions'][name];av=('direct',a['id'],tuple(sorted((p['id'],0) for p in a['params'].values())))
    return identity,(av,c['ttl'])


def verify(output):
    manifest=json.loads((output/'manifest.json').read_text());assert manifest['completed']
    verified=json.loads((output/'verification.json').read_text())
    for name,wanted in verified['files_sha256'].items():assert digest(output/name)==wanted,name
    schema=json.loads((output/'source/experiments/table-constraints/fixtures/schema.json').read_text())
    observations=[json.loads(s) for s in (output/'observations.jsonl').read_text().splitlines()]
    rpc=[json.loads(s) for s in (output/'rpc.jsonl').read_text().splitlines()];trace=defaultdict(list)
    for e in rpc:trace[e['token']].append(e)
    csv_rows=list(csv.DictReader((output/'case-results.csv').open()))
    indexed={(r['repetition'],r['profile'],r['case_id']):r for r in csv_rows}
    assert len(indexed)==len(observations)==manifest['observations']
    counts=Counter();accepted=0;normalizations=[];valid_errors=[];readback_anomalies=[]
    for r in observations:
        c=r['case'];events=trace.get(r['token'],[])
        if r['stage']=='not_expressible':assert not events;outcome='not_expressible'
        elif not events:
            assert r.get('error');outcome='local_panic' if 'adapter recovered panic:' in r['error'] else 'local_rejected'
        else:
            assert len(events)==1
            outcome='target_rejected' if events[0]['rpc_code'] else 'accepted'
            assert bool(r.get('error'))==(outcome=='target_rejected')
        row=indexed[str(r['repetition']),r['profile'],c['id']];assert row['outcome']==outcome
        before,other_before=snapshot(r['before']);after,other_after=snapshot(r['after']);assert other_before==other_after
        desired=before.copy()
        if outcome=='accepted':
            update=events[0]['request']['updates'][0];wire=update['entity']['table_entry'];identity,value=entry(wire)
            assert update['type']==c['update'];accepted+=1
            if c['update']=='DELETE':desired.pop(identity)
            else:
                if c['action_kind']=='none' and c['default']:
                    _,value=expected(schema,c)
                desired[identity]=value
            if c['category'] in ('valid','normalizable','policy'):
                want_k,want_v=expected(schema,c)
                assert identity==want_k,(r['token'],identity,want_k)
                if c['update']!='DELETE':assert value==want_v,(r['token'],value,want_v)
                if c['category'] in ('normalizable','policy'):normalizations.append(r['token'])
            if desired!=after:
                assert manifest['mode']=='bmv2' and c['id'] in ('ordinary-negative-priority','ttl-default-1'),r['token']
                adjusted=before.copy()
                if c['id']=='ordinary-negative-priority':
                    adjusted[(identity[0],False,0,identity[3])]=value
                else:adjusted[identity]=(value[0],0)
                assert after==adjusted,r['token']
                readback_anomalies.append(r['token'])
        else:assert before==after,r['token']
        if c['category'] in ('valid','normalizable') and outcome not in ('accepted','not_expressible'):
            valid_errors.append(r['token'])
        assert bool(row['state_matches_request']=='True')==(after==desired)
        counts[r['profile'],c['category'],outcome]+=1
    for name,wanted in manifest['source_sha256'].items():assert digest(output/'source'/name)==wanted,name
    return dict(directory=output.name,observations=len(observations),accepted_requests=accepted,
                full_state_comparisons=len(observations),valid_input_rejections=valid_errors,readback_anomalies=readback_anomalies,
                normalized_acceptances=normalizations,counts={'/'.join(k):v for k,v in sorted(counts.items())})
