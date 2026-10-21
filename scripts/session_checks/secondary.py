"""Secondary raw JSON checks for all four E3 groups."""

import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_entry(value):
    return dict(table=value['table_id'],match=sorted((m['field_id'],int.from_bytes(base64.b64decode(m['exact']['value']),'big')) for m in value.get('match',[])),
                action=value['action']['action']['action_id'],params=sorted((p['param_id'],int.from_bytes(base64.b64decode(p['value']),'big')) for p in value['action']['action'].get('params',[])),
                metadata=base64.b64decode(value.get('metadata','')))


def check_group(output):
    m=json.loads((output/'manifest.json').read_text());assert m['complete']
    for name,wanted in m['files_sha256'].items():assert digest(output/'source'/name)==wanted,name
    rows=[json.loads(s) for s in (output/'observations.jsonl').read_text().splitlines()]
    events={e['sequence']:e for e in map(json.loads,(output/'rpc.jsonl').read_text().splitlines())}
    assert len(events)==max(events)+1
    assert len(rows)==m['trials']==len(m['schedule'])
    configurations=Counter();packets=0;recoveries=0;raw_payloads=0
    for r,job in zip(rows,m['schedule']):
        assert all(r[k]==v for k,v in job.items())
        kind=m['kind'];configurations['/'.join(str(r[k]) for k in ('adapter','mode','path','delay_ms') if k in r)]+=1
        if kind=='recovery':
            assert r['driver_exit_code']==0
            before=raw_entry(r['initial_entries'][0]);assert before['params'][0][1]==1 and before['metadata']==b'session-write'
            if r['recovered']:
                recoveries+=1;after=raw_entry(r['final_entries'][0]);assert after['params'][0][1]==2 and after['metadata']==b'recovered'
                assert before['table']==after['table'] and before['match']==after['match'] and before['action']==after['action']
                fault=events[r['fault_sequence']]
                for metric,seq in [('arbitration','arbitration_sequence'),('primary_observed','primary_observed_sequence'),('successful_write','successful_write_sequence'),('verified_state','verified_sequence')]:
                    assert r['fault_to_'+metric+'_ns']==events[r[seq]]['monotonic_ns']-fault['monotonic_ns']>=0
                phase=[e for k,e in events.items() if r['start_sequence']<=k<=r['end_sequence']]
                assert not any(e['event']=='arbitration_gate_released' for e in phase)
                writes=[e for e in phase if e['event']=='write_end'];assert len(writes)==2 and all(e['rpc_code']==0 for e in writes)
                for e in writes:
                    wire=e['request']['updates'][0]['entity']['table_entry'];assert raw_entry(wire) in (before,after);raw_payloads+=1
                if r['mode']=='restart':assert r['pipeline_missing'] and not r['entries_after_install']
                else:assert r['preserved_entries']==r['initial_entries']
            else:assert r['censored'] and r['censor_ns']==60000000000
        elif kind=='mastership':
            assert not r['backup']['is_primary'] and r['primary']['is_primary']
            final=raw_entry(r['final_entries'][0]);assert final['params'][0][1]==1 and final['metadata']==b'session-write'
            assert r['initial']['rpc_code']==r['final_write']['rpc_code']==0
            assert r['backup_write_rpcs']==(0 if r['adapter']=='sdk' else 1)
            assert events[r['demotion_sequence']]['response']['arbitration']['status']['code']==6
            assert r['demotion_to_observation_ns']==events[r['backup_observed_sequence']]['monotonic_ns']-events[r['demotion_sequence']]['monotonic_ns']>=0
        elif kind=='cancellation':
            assert r['driver_exit_code']==0
            phase=[e for k,e in events.items() if r['start_sequence']<=k<=r['end_sequence']]
            received=[e['packet_id'] for e in phase if e['event']=='packet_out_received']
            assert received==r['received_packet_ids'] and len(received)==len(set(received))
            for e in phase:
                if e['event']=='packet_out_received':
                    payload=b'P4EXP001'+e['packet_id'].to_bytes(8,'big')+bytes(e['size']-16)
                    assert hashlib.sha256(payload).hexdigest()==e['sha256'];raw_payloads+=1
            if r['mode'] in ('pre_cancelled','queued_cancelled'):assert r['cancelled_send']['cancelled'] and 20000 not in received and 99999 in received
            elif r['mode']=='inflight_cancelled':assert r['cancelled_send']['cancelled'] and (99999 in received if r['recovered'] else True)
            elif r['mode']=='immediate':assert received==[]
            elif r['mode']=='graceful':
                assert received==[1] and r['request_eof_sequence']<r['final_status_released_sequence']
                final=[e for e in phase if e['event']=='stream_final_ok'];assert len(final)==1
                assert r['close']['monotonic_ns']>=final[0]['monotonic_ns']
            else:assert r['mode']=='graceful_timeout' and received==[1] and r['close']['deadline'] and not any(e['event']=='stream_final_ok' for e in phase)
        else:
            final=r['final'];assert r['driver_exit_code']==0 and not final['is_primary']
            arrived=final['arrived'];processed=final['processed'];assert arrived==sorted(set(arrived)) and processed==sorted(set(processed))
            phase=[e for k,e in events.items() if r['start_sequence']<=k<=r['end_sequence']]
            emitted=[e['packet_id'] for e in phase if e['event']=='packet_in_emit'];assert emitted==list(range(r['sent']));packets+=len(emitted)
            assert set(processed)<=set(arrived)<=set(emitted)
            assert len(processed)==r['delivered'] and len(arrived)-len(processed)==r['application_dropped']
            assert r['sent']-len(arrived)==r['not_delivered_to_iterator']
            assert len(processed)+r['application_dropped']+r['not_delivered_to_iterator']==r['sent']
            assert r['marker_to_observed_backup_ns']==events[r['observation_sequence']]['client_monotonic_ns']-events[r['marker_sequence']]['monotonic_ns']>=0
            assert r['peak_sampled_rss_kib']==max(v['rss_kib'] for v in r['rss_samples'])
    assert all(n==m['repetitions'] for n in configurations.values())
    return dict(group=m['kind'],trials=len(rows),recoveries=recoveries,packets_checked=packets,raw_payloads_checked=raw_payloads,
                repetitions_per_configuration=dict(configurations))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    groups=[check_group(p) for p in args.output.iterdir() if p.is_dir() and (p/'manifest.json').exists()]
    record=dict(verified_local=datetime.now().astimezone().isoformat(),decoder='stdlib JSON/base64/int, independent of protobuf and SDK encoders',
                trials=sum(g['trials'] for g in groups),packets_checked=sum(g['packets_checked'] for g in groups),groups=groups)
    (args.output/'independent-verification.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps({k:v for k,v in record.items() if k!='groups'}))


if __name__=='__main__':main()
