"""Run formal E3 stages sequentially with randomized per-round order."""

import argparse
from datetime import datetime
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import traceback

from common import ROOT,REPOSITORY,Trace,Target,Relay,snapshot,check_setup
import recover
import mastership
import cancellation
import consumers


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def configurations(plan,kind,adapters):
    if kind=='recovery':return [dict(adapter=a,mode=m) for a,m in itertools.product(adapters,plan['recovery_modes'])]
    if kind=='mastership':return [dict(adapter=a) for a in adapters]
    if kind=='cancellation':return [dict(adapter=a,mode=m) for a in adapters for m in plan['cancellation_modes'][a]]
    return [dict(adapter=a,path=p,delay_ms=d) for a,p,d in itertools.product(adapters,plan['consumer_paths'],plan['consumer_delays_ms'])]


def execute(kind,job,trace,output,plan):
    adapter=job['adapter'];rep=job['repetition']
    if kind=='recovery':return recover.trial(adapter,job['mode'],rep,trace,output)
    if kind=='cancellation':return cancellation.trial(adapter,job['mode'],rep,trace,output)
    if kind=='consumers':return consumers.trial(adapter,job['path'],job['delay_ms'],rep,plan['packet_count'],plan['offered_rate_pps'],trace,output)
    start=trace.add('trial_started',kind=kind,adapter=adapter,repetition=rep)
    target=Target(output/f'{adapter}-{rep}-target');relay=None
    try:
        target.start();relay=Relay(target.address,trace)
        result=mastership.trial(adapter,rep,relay,trace,output)
        result['start_sequence']=start['sequence']
        return result
    finally:
        if relay:relay.close()
        target.stop()
        if 'result' in locals():
            result['target_processes']=target.history
            result['end_sequence']=trace.add('trial_completed',kind=kind,adapter=adapter)['sequence']


def stage(kind,output,plan,args,queue_state):
    output.mkdir()
    adapters=['sdk'] if args.race else plan['adapters']
    repetitions=1 if args.race or args.smoke else plan['repetitions']
    rng=random.Random(plan['seed']+plan['groups'].index(kind));schedule=[]
    for rep in range(1,repetitions+1):
        block=configurations(plan,kind,adapters);rng.shuffle(block)
        schedule += [dict(**job,repetition=rep) for job in block]
    configuration=dict(kind=kind,repetitions=repetitions,schedule=schedule,adapters=adapters,complete=False,trials=0,started_local=datetime.now().astimezone().isoformat(),plan=plan,
                       smoke=args.smoke,race=args.race,packet_count=plan['packet_count'],offered_rate_pps=plan['offered_rate_pps'],
                       paths=plan['consumer_paths'],delays_ms=plan['consumer_delays_ms'],configurations={a:plan['cancellation_modes'][a] for a in adapters})
    if kind=='cancellation':configuration['backpressure_probe_seconds']=cancellation.BACKPRESSURE_PROBE_SECONDS
    snapshot(output,configuration)
    frozen=json.loads((output/'manifest.json').read_text())['files_sha256']
    trace=Trace(output/'rpc.jsonl');completed=0;targets=[]
    try:
        with (output/'observations.jsonl').open('w',buffering=1) as records:
            for job in schedule:
                queue_state.update(stage=kind,stage_completed=completed,stage_total=len(schedule),current=job,updated_local=datetime.now().astimezone().isoformat())
                (output.parent/'state.json').write_text(json.dumps(queue_state,indent=2)+'\n')
                result=execute(kind,job,trace,output,plan)
                records.write(json.dumps(result)+'\n');completed+=1
                targets+=result.get('target_processes',[])
                queue_state['completed']+=1
                print(json.dumps(dict(group=kind,completed=completed,total=len(schedule),job=job,queue_completed=queue_state['completed'])),flush=True)
        configuration.update(complete=True,trials=completed,completed_local=datetime.now().astimezone().isoformat())
    except BaseException:
        (output/'failure.txt').write_text(traceback.format_exc());raise
    finally:
        trace.close();configuration['trials']=completed
        configuration['files_sha256']=frozen
        record=json.loads((output/'manifest.json').read_text());record.update(configuration)
        (output/'manifest.json').write_text(json.dumps(record,indent=2)+'\n')
        (output/'target-processes.json').write_text(json.dumps(targets,indent=2)+'\n')
        for name,wanted in frozen.items():assert digest(REPOSITORY/name)==wanted,('execution source changed',name)


def main():
    parser=argparse.ArgumentParser()
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--smoke',action='store_true');modes.add_argument('--race',action='store_true')
    parser.add_argument('--groups',nargs='+',choices=['recovery','mastership','cancellation','consumers'])
    parser.add_argument('--output',type=Path);args=parser.parse_args()
    if not __debug__:parser.error("Run without -O so evidence checks remain enabled.")
    check_setup()
    plan=json.loads((ROOT/'formal-plan.json').read_text())
    for name in ('P4EXPERIMENT_SDK_BINARY','P4EXPERIMENT_SESSION_BINARY'):os.environ.pop(name,None)
    if args.race:
        os.environ['P4EXPERIMENT_SDK_BINARY']=str(ROOT/'bin/sdk-race')
        os.environ['P4EXPERIMENT_SESSION_BINARY']=str(ROOT/'bin/session-sdk-race')
    output=args.output or REPOSITORY/'results/sessions'/datetime.now().strftime(('race-' if args.race else 'smoke-' if args.smoke else 'formal-')+'%Y%m%d-%H%M%S')
    if any(output.resolve().is_relative_to(REPOSITORY/name) for name in ('data','figures')):
        parser.error("Output must be outside the archived data and figures.")
    output.mkdir(parents=True,exist_ok=False)
    groups=args.groups or plan['groups']
    if len(groups)!=len(set(groups)):parser.error('Select each group once.')
    state=dict(started_local=datetime.now().astimezone().isoformat(),pid=os.getpid(),output=str(output),groups=groups,completed=0,complete=False)
    (output/'state.json').write_text(json.dumps(state,indent=2)+'\n')
    try:
        for group in groups:
            stage(group,output/group,plan,args,state)
            state['stage_completed']=state['stage_total']
        check_setup()
        state.update(complete=True,completed_local=datetime.now().astimezone().isoformat())
    except BaseException:
        state['error']=traceback.format_exc();raise
    finally:(output/'state.json').write_text(json.dumps(state,indent=2)+'\n')
    print(json.dumps(dict(output=str(output),completed=state['completed'],complete=True)),flush=True)


if __name__=='__main__':main()
