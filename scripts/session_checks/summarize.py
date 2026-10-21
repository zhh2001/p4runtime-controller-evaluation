"""Summarize E3 trials, preserving configuration-level outcomes."""

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import statistics


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def quantile(values,p):
    x=sorted(values);at=(len(x)-1)*p;low=int(at);high=min(low+1,len(x)-1)
    return x[low]+(x[high]-x[low])*(at-low)


def summarize(values,key):
    rng=random.Random(20261019+int.from_bytes(hashlib.sha256(key.encode()).digest()[:4],'big'))
    boots=[statistics.median(rng.choices(values,k=len(values))) for _ in range(10000)]
    return dict(n=len(values),mean=statistics.mean(values),median=statistics.median(values),sample_stddev=statistics.stdev(values) if len(values)>1 else 0,
        minimum=min(values),maximum=max(values),p95=quantile(values,.95),median_ci_low=quantile(boots,.025),median_ci_high=quantile(boots,.975))


def read_rows(path):return [json.loads(s) for s in path.read_text().splitlines()]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();output=args.output.resolve()
    state=json.loads((output/'state.json').read_text());assert state['complete'] and state['completed']==840
    rows={g:read_rows(output/g/'observations.jsonl') for g in state['groups']}
    groups=defaultdict(list);outcomes=[]
    for r in rows['recovery']:
        if r['recovered']:
            for metric in ('fault_to_arbitration_ns','fault_to_primary_observed_ns','fault_to_successful_write_ns','fault_to_verified_state_ns'):
                groups['recovery',r['adapter'],r['mode'],metric.removesuffix('_ns')+'_ms'].append(r[metric]/1e6)
    for r in rows['mastership']:groups['mastership',r['adapter'],'takeover','demotion_to_observation_ms'].append(r['demotion_to_observation_ns']/1e6)
    for r in rows['consumers']:
        setting=r['path']+'/'+str(r['delay_ms'])
        for metric,value in dict(backup_observation_ms=r['marker_to_observed_backup_ns']/1e6,processed_count=r['delivered'],peak_rss_kib=r['peak_sampled_rss_kib']).items():
            groups['consumers',r['adapter'],setting,metric].append(value)
    stats=[dict(group=k[0],adapter=k[1],setting=k[2],metric=k[3],unit='ms' if k[3].endswith('_ms') else 'KiB' if k[3].endswith('_kib') else 'packets',**summarize(v,'/'.join(k))) for k,v in sorted(groups.items())]
    with (output/'statistics.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(stats[0]));writer.writeheader();writer.writerows(stats)
    by_config=defaultdict(list)
    for g,items in rows.items():
        for r in items:by_config[g,r['adapter'],r.get('mode',r.get('path','takeover'))+('/'+str(r['delay_ms']) if 'delay_ms' in r else '')].append(r)
    for (g,adapter,setting),items in sorted(by_config.items()):
        v=dict(group=g,adapter=adapter,setting=setting,trials=len(items))
        if g=='recovery':v.update(recovered=sum(r['recovered'] for r in items),censored=sum(r['censored'] for r in items))
        elif g=='mastership':v.update(primary_restored=sum(r['primary']['is_primary'] for r in items),backup_write_rpc=sum(r['backup_write_rpcs'] for r in items))
        elif g=='cancellation':v.update(session_recovered=sum(r.get('recovered',False) for r in items),received_packets=sum(len(r['received_packet_ids']) for r in items))
        else:v.update(sent=sum(r['sent'] for r in items),processed=sum(r['delivered'] for r in items),library_undelivered=sum(r['not_delivered_to_iterator'] for r in items),application_dropped=sum(r['application_dropped'] for r in items))
        outcomes.append(v)
    assert len(outcomes)==28 and all(r['trials']==30 for r in outcomes)
    summary=dict(created_local=datetime.now().astimezone().isoformat(),total_trials=840,groups={g:len(v) for g,v in rows.items()},outcomes=outcomes,
        statistics_rows=len(stats),quantile_method='linear, type 7',bootstrap=dict(statistic='median',replicates=10000,seed='20261019 + first 4 SHA256 bytes of configuration key',interval='percentile 95%'),
        consumer_totals={k:sum(r[k] for r in rows['consumers']) for k in ('sent','delivered','not_delivered_to_iterator','application_dropped')},
        inputs_sha256={str(p.relative_to(output)):digest(p) for g in state['groups'] for p in [output/g/'manifest.json',output/g/'observations.jsonl',output/g/'rpc.jsonl',output/g/'verification.json']},
        script_sha256=digest(Path(__file__)))
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('outcomes','inputs_sha256')}))


if __name__=='__main__':main()
