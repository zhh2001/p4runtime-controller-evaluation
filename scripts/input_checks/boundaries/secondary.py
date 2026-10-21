"""Independent boundary-value checks using JSON, base64 and integers."""

import base64
from collections import Counter
import csv
import json


def rows(path):return [json.loads(v) for v in path.read_text().splitlines()]


def integer(value,signed=False):return int.from_bytes(base64.b64decode(value),'big',signed=signed)


def data_value(data,expected,signed=False):
    if 'bitstring' in data:
        if isinstance(expected,dict) and 'text_bytes' in expected:
            return dict(text_bytes=base64.b64decode(data['bitstring']).hex())
        return integer(data['bitstring'],signed)
    if 'bool' in data:return data['bool']
    if 'varbit' in data:return dict(value=integer(data['varbit']['bitstring']),bitwidth=data['varbit'].get('bitwidth',0))
    field='tuple' if 'tuple' in data else 'struct'
    values=data[field]['members']
    assert len(values)==len(expected)
    return [data_value(actual,wanted) for actual,wanted in zip(values,expected)]


def verify(folder):
    checked=0
    wire_values=0
    observations=rows(folder/'observations.jsonl')
    requests={r['token']:r for r in rows(folder/'rpc.jsonl')}
    with (folder/'case-results.csv').open() as file:evaluations={ (r['profile'],int(r['repetition']),r['case_id']):r for r in csv.DictReader(file)}
    corpus=json.loads((folder/'source/experiments/boundaries/fixtures/cases.json').read_text())
    cases={c['id']:c for c in corpus}
    counts=Counter()
    for row in observations:
        case=cases[row['id']]
        assert case==row['case']
        result=evaluations[row['profile'],row['repetition'],case['id']]
        event=requests.get(row['token'])
        outcome=('not_expressible' if row['stage']=='not_expressible' else 'local_rejected') if not event else 'target_rejected' if event['rpc_code'] else 'accepted'
        assert result['outcome']==outcome,(row['token'],outcome,result['outcome'])
        counts[row['profile'],case['category']=='invalid',outcome]+=1
        if outcome=='accepted':
            assert case['category']!='invalid'
            request=event['request']
            if case['op']=='write':
                entity=request['updates'][0]['entity']
                if case['kind'] in ('match','param'):
                    entry=entity['table_entry']
                    assert integer(entry['match'][0]['exact']['value'])==case['expected']['key']
                    assert integer(entry['action']['action']['params'][0]['value'])==case['expected']['value']
                else:
                    entry=entity[case['kind']+'_entry']
                    assert int(entry.get('index',{}).get('index',0))==case['index']
                    if case['kind']=='register':
                        assert data_value(entry['data'],case['expected'],case['family']=='register_signed')==case['expected'],row['token']
                    elif case['kind']=='counter':
                        assert int(entry['data']['packet_count'])==11 and int(entry['data']['byte_count'])==704
                    else:
                        assert {k:int(v) for k,v in entry['config'].items()}==dict(cir=10,pir=20,cburst=2,pburst=4)
                wire_values+=1
            else:
                entry=request['entities'][0][case['kind']+'_entry']
                if case['index']==-1:assert 'index' not in entry
                else:assert int(entry.get('index',{}).get('index',0))==case['index']
        checked+=1
    return dict(observations_checked=checked, accepted_write_payloads_checked=wire_values,
                counts={"/".join(map(str,k)):v for k,v in sorted(counts.items())})
