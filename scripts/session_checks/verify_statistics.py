"""NumPy recomputation of E3 descriptive statistics and median intervals."""

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random

import numpy as np


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();out=args.output
    groups=defaultdict(list)
    for kind in ('recovery','mastership','consumers'):
        records=[json.loads(v) for v in (out/kind/'observations.jsonl').read_text().splitlines()]
        for r in records:
            if kind=='recovery' and r['recovered']:
                for m in ('fault_to_arbitration_ns','fault_to_primary_observed_ns','fault_to_successful_write_ns','fault_to_verified_state_ns'):
                    groups[kind,r['adapter'],r['mode'],m.removesuffix('_ns')+'_ms'].append(r[m]/1e6)
            elif kind=='mastership':groups[kind,r['adapter'],'takeover','demotion_to_observation_ms'].append(r['demotion_to_observation_ns']/1e6)
            elif kind=='consumers':
                setting=r['path']+'/'+str(r['delay_ms'])
                for m,v in [('backup_observation_ms',r['marker_to_observed_backup_ns']/1e6),('processed_count',r['delivered']),('peak_rss_kib',r['peak_sampled_rss_kib'])]:groups[kind,r['adapter'],setting,m].append(v)
    checked=0
    rows=list(csv.DictReader((out/'statistics.csv').open()))
    for row in rows:
        key=(row['group'],row['adapter'],row['setting'],row['metric']);a=np.array(groups[key]);assert len(a)==int(row['n'])
        rng=random.Random(20261019+int.from_bytes(hashlib.sha256('/'.join(key).encode()).digest()[:4],'big'))
        uniforms=np.array([rng.random() for _ in range(10000*len(a))]).reshape(10000,len(a))
        samples=a[np.floor(uniforms*len(a)).astype(int)]
        low,high=np.quantile(np.median(samples,axis=1),[.025,.975],method='linear')
        expected=[np.mean(a),np.median(a),np.std(a,ddof=1),np.min(a),np.max(a),np.quantile(a,.95,method='linear'),low,high]
        actual=[float(row[k]) for k in ('mean','median','sample_stddev','minimum','maximum','p95','median_ci_low','median_ci_high')]
        np.testing.assert_allclose(actual,expected,rtol=1e-10,atol=1e-8);checked+=len(actual)
    result=dict(verified_local=datetime.now().astimezone().isoformat(),numpy=np.__version__,summary_rows=len(rows),numbers_checked=checked,independent_recomputation=True)
    (out/'statistics-verification.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
