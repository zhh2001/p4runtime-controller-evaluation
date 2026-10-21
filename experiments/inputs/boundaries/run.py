"""Execute the fixed corpus and record requests independently of the adapters."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import grpc
from finsy.proto import p4i, p4r, p4r_grpc
from google.protobuf import text_format

from endpoint import Endpoint, message
from fixtures import ROOT, WIDTHS


REPOSITORY = ROOT.parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
from bootstrap_inputs import check_versions, snapshot_run, check_setup
FINSY = REPOSITORY/".runtime/finsy/bin/python"
SHELL = REPOSITORY/".runtime/shell/bin/python"
PROFILES = ["sdk", "raw-current", "legacy", "raw-legacy", "finsy", "shell"]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Driver:
    def __init__(self, profile, address, folder, race):
        self.stderr = (folder/(profile+".stderr")).open("w")
        if profile in ("sdk", "raw-current", "legacy", "raw-legacy"):
            stack = "sdk" if profile in ("sdk", "raw-current") else "legacy"
            command = [str(ROOT/"bin"/("go-"+stack+("-race" if race else "")))]
        else:
            command = [str(FINSY if profile=="finsy" else SHELL), str(ROOT/"python_driver.py")]
        command += ["--profile", profile, "--address", address, "--p4info", str(ROOT/"fixtures/controlled.p4info.txt")]
        (folder/(profile+"-command.json")).write_text(json.dumps(command, indent=2)+'\n')
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1)
        self.records = queue.Queue()
        self.stdout = (folder/(profile+".jsonl")).open("w", buffering=1)
        def read():
            for line in self.process.stdout:
                self.stdout.write(line)
                try: self.records.put(json.loads(line))
                except ValueError: self.records.put(dict(fatal="invalid driver JSON", line=line))
            self.records.put(dict(fatal="driver ended"))
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        try:
            assert self.receive().get("ready")
        except BaseException:
            self.process.kill()
            self.process.wait()
            self.thread.join(timeout=5)
            self.stdout.close();self.stderr.close()
            self.process.stdin.close();self.process.stdout.close()
            raise

    def receive(self):
        row = self.records.get(timeout=30)
        assert not row.get("fatal"), row
        return row

    def execute(self, case):
        self.process.stdin.write(json.dumps(case, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        return self.receive()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write('{"op":"close"}\n')
            self.process.stdin.flush()
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=5)
        self.stdout.close()
        self.stderr.close()
        self.process.stdin.close()
        self.process.stdout.close()
        assert not self.thread.is_alive()
        return self.process.returncode


class TargetControl:
    def __init__(self, target, info):
        self.info = info
        self.channel = grpc.insecure_channel(target.address)
        self.stub = p4r_grpc.P4RuntimeStub(self.channel)

    def initialize(self):
        requests = queue.Queue()
        def iterator():
            while (value := requests.get()) is not None: yield value
        stream = self.stub.StreamChannel(iterator())
        requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1, election_id=p4r.Uint128(low=1))))
        assert next(stream).arbitration.status.code == 0
        base_info = text_format.Parse((ROOT/"fixtures/target.p4info.txt").read_text(),p4i.P4Info())
        self.stub.SetForwardingPipelineConfig(p4r.SetForwardingPipelineConfigRequest(device_id=1,election_id=p4r.Uint128(low=1),
            action=p4r.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,
            config=p4r.ForwardingPipelineConfig(p4info=base_info,p4_device_config=(ROOT/"fixtures/boundaries.json").read_bytes())),timeout=10)
        updates=[]
        for width in WIDTHS:
            table=next(t for t in self.info.tables if t.preamble.name==f"MyIngress.t_w{width}")
            action=next(a for a in self.info.actions if a.preamble.name==f"MyIngress.a_w{width}")
            for key in sorted({0,1,(1<<width)-1}):
                value=key.to_bytes(max(1,(key.bit_length()+7)//8),"big")
                entry=p4r.TableEntry(table_id=table.preamble.id,match=[p4r.FieldMatch(field_id=table.match_fields[0].id,exact=p4r.FieldMatch.Exact(value=value))],
                    action=p4r.TableAction(action=p4r.Action(action_id=action.preamble.id,params=[p4r.Action.Param(param_id=action.params[0].id,value=b'\x01')])))
                updates.append(p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(table_entry=entry)))
        self.stub.Write(p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=updates),timeout=5)
        requests.put(None)
        stream.cancel()

    def snapshot(self):
        selectors=[p4r.Entity(table_entry=p4r.TableEntry()),p4r.Entity(counter_entry=p4r.CounterEntry()),p4r.Entity(meter_entry=p4r.MeterEntry())]
        entities=[e for reply in self.stub.Read(p4r.ReadRequest(device_id=1,entities=selectors),timeout=5) for e in reply.entities
                  if not (e.WhichOneof('entity')=='table_entry' and e.table_entry.is_default_action)]
        return sorted([message(e) for e in entities],key=lambda e:json.dumps(e,sort_keys=True))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('controlled','bmv2'),default='controlled')
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--race',action='store_true')
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    if not __debug__: parser.error("Run without -O so semantic checks remain enabled.")
    output=args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases=json.loads((ROOT/'fixtures/cases.json').read_text())
    if args.mode=='bmv2':
        cases=[c for c in cases if c['family'].startswith('wide_') or c['family']=='resource_index' and not c['translated'] and c['kind']!='register']
        assert len(cases)==104
    if args.smoke or args.race:
        selected=[]
        for case in cases:
            if case['id'] in {'match-1-zero','match-129-max','param-9-overflow','param-128-padded','uint-129-max',
                               'signed-9-min','signed-129-overflow','typed-tuple-valid','typed-struct-missing','typed-text-empty','typed-varbit-nine','typed-nested-valid'}:
                selected.append(case)
            elif case['family']=='resource_index' and case['index'] in (-2,-1,7,8,10000):
                selected.append(case)
        cases=selected
    profiles=['sdk','legacy'] if args.race else PROFILES
    repetitions=1 if args.smoke or args.race else 2
    versions=check_versions()
    hashes=snapshot_run(output, "boundaries")
    manifest=dict(started_local=datetime.now().astimezone().isoformat(),mode=args.mode,smoke=args.smoke,race=args.race,
                  case_ids=[c['id'] for c in cases],profiles=profiles,repetitions=repetitions,source_sha256=hashes,
                  versions=versions,completed=False,observations=0,driver_exits=[])
    def save():
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    save()
    info=text_format.Parse((ROOT/'fixtures/controlled.p4info.txt').read_text(),p4i.P4Info())
    target=None;observer=None
    if args.mode=='bmv2':
        sys.path.insert(0,str(ROOT.parent/'common'))
        from infra import Target
        target=Target(output/'target');target.start()
        observer=TargetControl(target,info)
        try:observer.initialize()
        except BaseException:
            observer.channel.close();target.stop()
            (output/'target-processes.json').write_text(json.dumps(target.history,indent=2)+'\n')
            raise
    endpoint=Endpoint(info,output/'rpc.jsonl',target.address if target else None)
    server=grpc.server(ThreadPoolExecutor(max_workers=16))
    p4r_grpc.add_P4RuntimeServicer_to_server(endpoint,server)
    port=server.add_insecure_port('127.0.0.1:0');server.start()
    save()
    try:
        with (output/'observations.jsonl').open('w',buffering=1) as records:
            for repetition in range(1,repetitions+1):
                folder=output/f'round-{repetition}';folder.mkdir()
                for profile in profiles:
                    endpoint.token=None
                    driver=Driver(profile,f'127.0.0.1:{port}',folder,args.race)
                    try:
                        for case in cases:
                            token=f"{repetition}|{profile}|{case['id']}"
                            before=observer.snapshot() if observer else None
                            endpoint.token=token
                            row=driver.execute(dict(**case,token=token))
                            assert row['token']==token
                            after=observer.snapshot() if observer else None
                            trace=[r for r in endpoint.records if r['token']==token]
                            row.update(profile=profile,repetition=repetition,case=case,mode=args.mode,rpc_sequences=[r['sequence'] for r in trace],
                                       before=before,after=after)
                            records.write(json.dumps(row,ensure_ascii=False)+'\n')
                            manifest['observations']+=1
                    finally:
                        endpoint.token=None
                        code=driver.close();manifest['driver_exits'].append(dict(profile=profile,repetition=repetition,code=code))
                        assert code==0,(profile,code)
                    save()
                    print(json.dumps(dict(output=str(output),observations=manifest['observations'],profile=profile,repetition=repetition)),flush=True)
        check_setup()
        manifest['completed']=True
        manifest['completed_local']=datetime.now().astimezone().isoformat()
    finally:
        server.stop(0).wait(10)
        endpoint.close()
        if observer:observer.channel.close()
        if target:
            target.stop();(output/'target-processes.json').write_text(json.dumps(target.history,indent=2)+'\n')
        save()
    print(json.dumps(dict(output=str(output),completed=manifest['completed'],observations=manifest['observations'])),flush=True)


if __name__=='__main__':main()
