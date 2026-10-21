"""Run failed-only retry applications and independently observe target state."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

import grpc
from finsy.proto import p4r,p4r_grpc

from endpoint import Endpoint,message,binary
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from retry_checks.model import PROFILES,retry_plan,state_map
from retry_checks.policy import complete_codes

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / 'scripts'))
from bootstrap_retries import snapshot_run
from infra import INFO, CONFIG, Target, Session, entry

FINSY = REPOSITORY / '.runtime/finsy/bin/python'
SHELL = REPOSITORY / '.runtime/shell/bin/python'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def wire(op):
    value=entry(op['key'],op['metadata'].encode())
    value.action.action.params[0].value=op['port'].to_bytes(1,'big')
    if op['type']=='DELETE':value.ClearField('action');value.ClearField('metadata')
    return p4r.Update(type=getattr(p4r.Update,op['type']),entity=p4r.Entity(table_entry=value))


class Driver:
    def __init__(self,profile,address,folder,race):
        self.profile=profile
        if profile in ('sdk','raw-current','legacy','raw-legacy'):
            stack='sdk' if profile in ('sdk','raw-current') else 'legacy'
            command=[str(ROOT/'bin'/('go-'+stack+('-race' if race else '')))]
        else:command=[str(FINSY if profile=='finsy' else SHELL),str(ROOT/'python_driver.py')]
        command += ['--profile',profile,'--address',address]
        (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
        self.stderr=(folder/'driver.stderr').open('w');self.stdout=(folder/'driver.jsonl').open('w',buffering=1)
        try:self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,text=True,bufsize=1)
        except BaseException:
            self.stderr.close();self.stdout.close()
            raise
        self.rows=queue.Queue();self.timed_out=False
        def read():
            for line in self.process.stdout:
                self.stdout.write(line)
                try:self.rows.put(json.loads(line))
                except ValueError:self.rows.put(dict(fatal='driver invalid JSON',line=line))
            self.rows.put(dict(fatal='driver ended'))
        self.thread=threading.Thread(target=read,daemon=True);self.thread.start()
        try:assert self.receive(20).get('ready')
        except BaseException:self.close(force=True);raise

    def receive(self,timeout):
        row=self.rows.get(timeout=timeout);assert not row.get('fatal'),row;return row

    def execute(self,command,timeout):
        self.process.stdin.write(json.dumps(command)+'\n');self.process.stdin.flush()
        try:return self.receive(timeout)
        except queue.Empty:
            self.timed_out=True;self.process.kill();self.process.wait(timeout=5)
            return dict(token=command['token'],watchdog_timeout=True,watchdog_seconds=timeout)

    def close(self,force=False):
        if self.process.poll() is None:
            if force:self.process.kill()
            else:
                try:
                    self.process.stdin.write('{"op":"close"}\n');self.process.stdin.flush()
                except (BrokenPipeError, OSError):self.process.kill()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.thread.join(timeout=5);assert not self.thread.is_alive()
        try:self.process.stdin.close()
        except BrokenPipeError:pass
        self.process.stdout.close();self.stderr.close();self.stdout.close()
        return self.process.returncode


def snapshot(endpoint,observer):
    if observer:
        entities=[p4r.Entity(table_entry=e) for e in observer.read()]
    else:entities=[p4r.Entity(table_entry=e) for e in endpoint.state.values()]
    return dict(entities=[message(e) for e in entities],binary=[binary(e) for e in entities])


def trial(profile,case,token,endpoint,address,folder,race,plan,observer):
    row=dict(profile=profile,case_id=case['id'],token=token,case=case,steps=[],blocked=[],
             before=snapshot(endpoint,observer))
    pending=case['updates'];driver=Driver(profile,address,folder,race)
    try:
        for attempt in range(1,plan['max_attempts']+1):
            if not pending:break
            command=dict(op='write',token=token,updates=[message(wire(op)) for op in pending])
            result=driver.execute(command,plan['watchdog_seconds'])
            step=dict(attempt=attempt,pending=pending,result=result,after=snapshot(endpoint,observer))
            row['steps'].append(step)
            if result.get('watchdog_timeout'):
                row['outcome']='watchdog_timeout';break
            codes,origin=complete_codes(result,len(pending),profile)
            step.update(codes=codes,status_source=origin)
            decision=retry_plan(pending,codes)
            if decision.get('read_required'):
                read=driver.execute(dict(op='read',token=token),plan['watchdog_seconds'])
                assert not read.get('watchdog_timeout'),read
                step['application_read']=read
                decision=retry_plan(pending,codes,state_map(read['entities']))
            step['decision']=decision;row['blocked']+=decision['blocked'];pending=decision['pending']
        else:raise AssertionError('retry attempt bound exhausted')
        if 'outcome' not in row:row['outcome']='blocked' if row['blocked'] else 'completed'
        row['after']=snapshot(endpoint,observer)
    finally:row['driver_exit']=driver.close()
    row['rpc_sequences']=[r['sequence'] for r in endpoint.records if r['token']==token]
    return row


def main():
    from bootstrap_retries import selection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('controlled', 'bmv2'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--smoke', action='store_true')
    group.add_argument('--race', action='store_true')
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    output = args.output.resolve()
    if any(output.is_relative_to(REPOSITORY / name) for name in ('data', 'figures')):
        parser.error('Output must be outside archived data and figures.')
    mode = 'race' if args.race else 'smoke' if args.smoke else 'full'
    plan, cases, profiles, repetitions = selection(args.mode, mode)
    output.mkdir(parents=True, exist_ok=False)
    setup, hashes = snapshot_run(output)
    manifest = dict(started_local=datetime.now().astimezone().isoformat(), mode=args.mode,
                    smoke=args.smoke, race=args.race, profiles=profiles, case_ids=[case['id'] for case in cases],
                    repetitions=repetitions, plan=plan, versions=setup['versions'], source_sha256=hashes,
                    completed=False, observations=0,
                    host=dict(kernel=os.uname().release, meminfo=Path('/proc/meminfo').read_text()))
    def save():
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    save()
    target = observer = endpoint = server = None
    try:
        if args.mode == 'bmv2':
            target = Target(output / 'target')
            target.start()
            observer = Session(target.address)
        endpoint = Endpoint(INFO, output / 'rpc.jsonl', target.address if target else None)
        server = grpc.server(ThreadPoolExecutor(max_workers=16))
        p4r_grpc.add_P4RuntimeServicer_to_server(endpoint, server)
        port = server.add_insecure_port('127.0.0.1:0')
        if not port:
            raise RuntimeError('Could not bind the retry service')
        server.start()
        with (output / 'observations.jsonl').open('w', buffering=1) as results:
            for repetition in range(1, repetitions + 1):
                for profile in profiles:
                    for case in cases:
                        token = f"{repetition}|{profile}|{case['id']}"
                        folder = output / token.replace('|', '__')
                        folder.mkdir()
                        endpoint.configure(token, case)
                        if target:
                            session = Session(target.address, arbitrate=True)
                            try:
                                session.install()
                                if case['seeds']:
                                    result = session.write([wire(dict(seed, type='INSERT')) for seed in case['seeds']])
                                    assert result['rpc_code'] == 0, result
                            finally:
                                session.close()
                        row = trial(profile, case, token, endpoint, f'127.0.0.1:{port}', folder,
                                    args.race, plan, observer)
                        row.update(repetition=repetition, mode=args.mode)
                        results.write(json.dumps(row) + '\n')
                        assert row['driver_exit'] == (-9 if row['outcome'] == 'watchdog_timeout' else 0), row
                        manifest['observations'] += 1
                        save()
                    print(json.dumps(dict(repetition=repetition, profile=profile,
                                          observations=manifest['observations'])), flush=True)
        manifest.update(completed=True, finished_local=datetime.now().astimezone().isoformat())
        save()
    finally:
        if server:
            server.stop(0).wait(timeout=5)
        if endpoint:
            endpoint.close()
        if observer:
            observer.close()
        if target:
            target.stop()
            (output / 'target-processes.json').write_text(json.dumps(target.history, indent=2) + '\n')
        files = [path for path in output.rglob('*') if path.is_file() and path.name != 'artifact-hashes.json']
        (output / 'artifact-hashes.json').write_text(json.dumps(
            {str(path.relative_to(output)): digest(path) for path in files}, indent=2) + '\n')
    print(json.dumps(dict(output=str(output), completed=True, observations=manifest['observations'])), flush=True)


if __name__ == '__main__':
    main()


