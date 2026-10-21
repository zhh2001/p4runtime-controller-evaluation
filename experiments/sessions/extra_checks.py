"""Cross-check copied outcomes against the complete raw RPC event slices."""

import argparse
from datetime import datetime
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();out=args.output
    compared=0
    for group in ('recovery','mastership'):
        if not (out/group/'observations.jsonl').exists():continue
        rows=[json.loads(v) for v in (out/group/'observations.jsonl').read_text().splitlines()]
        events=[json.loads(v) for v in (out/group/'rpc.jsonl').read_text().splitlines()]
        for row in rows:
            if group=='recovery' and not row['recovered']:continue
            writes=[e for e in events if row['start_sequence']<=e['sequence']<=row['end_sequence'] and e['event']=='write_end']
            assert writes==row['writes']
            wanted=[0,7,0] if group=='mastership' and row['adapter']=='finsy' else [0,0]
            assert [e['rpc_code'] for e in writes]==wanted
            if group=='mastership':
                before=[e for e in writes if e['sequence']<row['demotion_sequence']]
                during=[e for e in writes if row['backup_observed_sequence']<e['sequence']<row['higher_closed_sequence']]
                after=[e for e in writes if e['sequence']>row['higher_closed_sequence']]
                assert len(before)==len(after)==1
                assert len(during)==row['backup_write_rpcs']==(1 if row['adapter']=='finsy' else 0)
            compared+=len(writes)
    record=dict(verified_local=datetime.now().astimezone().isoformat(),raw_writes_checked=compared,outcome_copies_match_rpc=True,backup_phase_writes_verified=True)
    (out/'extra-verification.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))


if __name__=='__main__':main()
