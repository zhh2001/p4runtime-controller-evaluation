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
from fixtures import ROOT
from model import initial_entries, lookup, default_entry, key, semantics


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
        command += ["--profile", profile, "--address", address, "--p4info", str(ROOT/"fixtures/target.p4info.txt")]
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
            config=p4r.ForwardingPipelineConfig(p4info=base_info,p4_device_config=(ROOT/"fixtures/tables.json").read_bytes())),timeout=10)
        updates=[]
        for profile in self.info.action_profiles:
            common=lookup(self.info,'actions','MyIngress.common')
            for member_id in (1,2):
                member=p4r.ActionProfileMember(action_profile_id=profile.preamble.id,member_id=member_id,
                    action=p4r.Action(action_id=common.preamble.id,params=[p4r.Action.Param(param_id=common.params[0].id,value=bytes([member_id]))]))
                updates.append(p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(action_profile_member=member)))
            if profile.with_selector:
                group=p4r.ActionProfileGroup(action_profile_id=profile.preamble.id,group_id=1,max_size=4,
                    members=[p4r.ActionProfileGroup.Member(member_id=1,weight=1)])
                updates.append(p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(action_profile_group=group)))
        for entry in initial_entries(self.info):
            table=next(t for t in self.info.tables if t.preamble.id==entry.table_id)
            if not entry.is_default_action and not table.is_const_table:
                updates.append(p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(table_entry=entry)))
        self.stub.Write(p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=updates),timeout=5)
        requests.put(None)
        stream.cancel()

    def snapshot(self):
        selectors=[p4r.Entity(table_entry=p4r.TableEntry())]
        selectors += [p4r.Entity(table_entry=p4r.TableEntry(table_id=t.preamble.id,is_default_action=True)) for t in self.info.tables]
        selectors += [p4r.Entity(action_profile_member=p4r.ActionProfileMember()),p4r.Entity(action_profile_group=p4r.ActionProfileGroup())]
        entities=[e for reply in self.stub.Read(p4r.ReadRequest(device_id=1,entities=selectors),timeout=5) for e in reply.entities]
        unique={json.dumps(message(e),sort_keys=True):message(e) for e in entities}
        return [unique[k] for k in sorted(unique)]

    def reset(self,case):
        table=lookup(self.info,'tables',case['table'])
        updates=[]
        if not table.is_const_table:
            selector=p4r.Entity(table_entry=p4r.TableEntry(table_id=table.preamble.id))
            entries=[e for reply in self.stub.Read(p4r.ReadRequest(device_id=1,entities=[selector]),timeout=5) for e in reply.entities if not e.table_entry.is_default_action]
            updates += [p4r.Update(type=p4r.Update.DELETE,entity=e) for e in entries]
            seed=next(e for e in initial_entries(self.info) if e.table_id==table.preamble.id and not e.is_default_action)
            updates.append(p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(table_entry=seed)))
        if not table.const_default_action_id and not table.implementation_id:
            seed=default_entry(self.info,table)
            if case['category']=='valid' and case['default'] and case['action_kind']=='none':
                seed.action.action.params[0].value=b'\x07'
            updates.append(p4r.Update(type=p4r.Update.MODIFY,entity=p4r.Entity(table_entry=seed)))
        if updates:
            request=p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=updates)
            self.stub.Write(request,timeout=5)
            return message(request)
        return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('controlled','bmv2'),default='controlled')
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--race',action='store_true')
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    if not __debug__: parser.error("Run without -O so semantic checks remain enabled.")
    output=args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases=json.loads((ROOT/'fixtures/cases.json').read_text())
    plan=json.loads((ROOT/'formal-plan.json').read_text())
    assert len(cases)==plan['cases'] and digest(ROOT/'fixtures/cases.json')==plan['cases_sha256']
    if args.smoke or args.race:
        names={'default-modify','default-reset','default-insert','default-stale-match','default-stale-priority',
               'ordinary-delete','delete-ignored-action-ttl','constant-default-same-action','constant-entry-modify',
               'constant-table-default-modify','scope-table_action-default','scope-default_action-ordinary',
               'indirect-member-1','indirect-direct-1','selected-group-1','ttl-idle-1','ttl-direct-1',
               'ttl-default--1','ternary-priority-10','sparse-normal','sparse-reverse','sparse-unknown-field'}
        cases=[c for c in cases if c['id'] in names]
    profiles=['sdk','legacy'] if args.race else PROFILES
    repetitions=1 if args.smoke or args.race else plan['repetitions']
    versions=check_versions()
    hashes=snapshot_run(output, "constraints")
    manifest=dict(started_local=datetime.now().astimezone().isoformat(),mode=args.mode,smoke=args.smoke,race=args.race,preflight=args.preflight,
                  case_ids=[c['id'] for c in cases],profiles=profiles,repetitions=repetitions,source_sha256=hashes,
                  versions=versions,completed=False,observations=0,driver_exits=[])
    manifest['plan']=plan
    def save():
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    save()
    info=text_format.Parse((ROOT/'fixtures/target.p4info.txt').read_text(),p4i.P4Info())
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
                            setup=observer.reset(case) if observer else endpoint.reset(case)
                            before=observer.snapshot() if observer else endpoint.snapshot()
                            endpoint.token=token
                            row=driver.execute(dict(**case,token=token))
                            assert row['token']==token
                            assert not endpoint.failures,endpoint.failures
                            after=observer.snapshot() if observer else endpoint.snapshot()
                            trace=[r for r in endpoint.records if r['token']==token]
                            row.update(profile=profile,repetition=repetition,case=case,mode=args.mode,rpc_sequences=[r['sequence'] for r in trace],
                                       before=before,after=after,setup=setup)
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
        check_setup()
    finally:
        server.stop(0).wait(10)
        endpoint.close()
        if observer:observer.channel.close()
        if target:
            target.stop();(output/'target-processes.json').write_text(json.dumps(target.history,indent=2)+'\n')
        save()
    print(json.dumps(dict(output=str(output),completed=manifest['completed'],observations=manifest['observations'])),flush=True)


if __name__=='__main__':main()
