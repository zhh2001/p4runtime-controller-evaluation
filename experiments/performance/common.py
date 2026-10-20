import hashlib
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

import grpc
from finsy.proto import p4i, p4r, p4r_grpc
from google.protobuf import json_format, text_format

ROOT = Path(__file__).resolve().parent
PAPER = ROOT.parent.parent
from infra import Target, Trace, status_record

INFO = text_format.Parse((ROOT/"fixtures/control.p4info.txt").read_text(),p4i.P4Info())
CONFIG = (ROOT/"fixtures/control.json").read_bytes()
TABLE = next(item for item in INFO.tables if item.preamble.name=="MyIngress.forwarding")
ACTION = next(item for item in INFO.actions if item.preamble.name=="MyIngress.forward")
PYTHON = PAPER/".runtime/finsy/bin/python"
SHELL = PAPER/".runtime/shell/bin/python"
MARKER = b"E2"
DEADLINE = 10
PROFILES = ["sdk","raw-current","legacy","raw-legacy","finsy","shell"]
CONFIGURATIONS = [(256,16,1),(4096,16,1),(16384,16,1),(4096,1,1),
                  (4096,128,1),(4096,16,4),(4096,16,16)]


def message(value):
    return json_format.MessageToDict(value,preserving_proto_field_name=True)


def entry(key,port=1):
    return p4r.TableEntry(table_id=TABLE.preamble.id,match=[p4r.FieldMatch(field_id=TABLE.match_fields[0].id,
        exact=p4r.FieldMatch.Exact(value=key.to_bytes(max(1,(key.bit_length()+7)//8),"big")))],
        action=p4r.TableAction(action=p4r.Action(action_id=ACTION.preamble.id,params=[
            p4r.Action.Param(param_id=ACTION.params[0].id,value=bytes([port]))])),metadata=MARKER)


class Control:
    def __init__(self,address,arbitrate=False):
        self.channel = grpc.insecure_channel(address)
        self.stub = p4r_grpc.P4RuntimeStub(self.channel)
        self.call = None
        if arbitrate:
            self.queue = queue.Queue()
            def requests():
                while (item:=self.queue.get()) is not None:
                    yield item
            self.call = self.stub.StreamChannel(requests())
            self.queue.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(
                device_id=1,election_id=p4r.Uint128(low=1))))
            assert next(self.call).arbitration.status.code==0

    def install(self):
        self.stub.SetForwardingPipelineConfig(p4r.SetForwardingPipelineConfigRequest(
            device_id=1,election_id=p4r.Uint128(low=1),
            action=p4r.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,
            config=p4r.ForwardingPipelineConfig(p4info=INFO,p4_device_config=CONFIG)),timeout=DEADLINE)

    def write(self,keys,port=1,kind=p4r.Update.INSERT):
        request=p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=[
            p4r.Update(type=kind,entity=p4r.Entity(table_entry=entry(key,port))) for key in keys])
        try:
            self.stub.Write(request,timeout=DEADLINE)
            return status_record(None)
        except grpc.RpcError as error:
            return status_record(error)

    def read(self):
        request=p4r.ReadRequest(device_id=1,entities=[p4r.Entity(table_entry=p4r.TableEntry(table_id=TABLE.preamble.id))])
        return [entity.table_entry for response in self.stub.Read(request,timeout=DEADLINE)
                for entity in response.entities if entity.HasField("table_entry") and not entity.table_entry.is_default_action]

    def close(self):
        if self.call is not None:
            self.queue.put(None)
            self.call.cancel()
        self.channel.close()


def decode_entries(entries):
    result={}
    for item in entries:
        assert item.table_id==TABLE.preamble.id and len(item.match)==1
        match=item.match[0]
        assert match.field_id==TABLE.match_fields[0].id and match.WhichOneof("field_match_type")=="exact"
        key=int.from_bytes(match.exact.value,"big")
        assert key not in result and 0 < key <= 32769
        action=item.action.action
        assert action.action_id==ACTION.preamble.id and len(action.params)==1
        assert action.params[0].param_id==ACTION.params[0].id and item.metadata==MARKER
        result[key]=int.from_bytes(action.params[0].value,"big")
    return result


class Driver:
    def __init__(self,profile,address,log):
        self.profile=profile
        self.log=open(log,"w")
        if profile in ("sdk","raw-current"):
            command=[os.environ.get("P4PERF_SDK_BINARY",str(ROOT/"bin/go-sdk")),"--profile",profile]
        elif profile in ("legacy","raw-legacy"):
            command=[os.environ.get("P4PERF_LEGACY_BINARY",str(ROOT/"bin/go-legacy")),"--profile",profile]
        else:
            command=[str(PYTHON if profile=="finsy" else SHELL),str(ROOT/"python_driver.py"),"--profile",profile]
        command+=["--address",address,"--p4info",str(ROOT/"fixtures/control.p4info.txt")]
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=self.log,text=True,bufsize=1)
        self.rows=queue.Queue()
        def reader():
            for line in self.process.stdout:
                try:self.rows.put(json.loads(line))
                except ValueError:self.rows.put({"fatal":"invalid stdout","value":line})
            self.rows.put({"fatal":"process ended"})
        self.thread=threading.Thread(target=reader,daemon=True)
        self.thread.start()
        self.ready=self.receive()
        assert self.ready.get("ready"),self.ready

    def receive(self):
        row=self.rows.get(timeout=120)
        if row.get("fatal"):raise RuntimeError(row)
        return row

    def command(self,**command):
        self.process.stdin.write(json.dumps(command)+"\n")
        self.process.stdin.flush()
        return self.receive()

    def close(self):
        if self.process.poll() is None:
            try:self.command(op="close")
            except (BrokenPipeError,RuntimeError,queue.Empty):pass
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=5)
        self.thread.join(timeout=1)
        assert not self.thread.is_alive()
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()


def snapshot(output,configuration):
    files=[path for path in ROOT.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    files+=list((PAPER/"config").glob("*"))
    files+=list((PAPER/"scripts").glob("*.py"))
    files+=[PAPER/".runtime/setup.json"]
    hashes={}
    for path in files:
        name=str(path.relative_to(PAPER))
        data=path.read_bytes()
        target=output/"source"/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
        hashes[name]=hashlib.sha256(data).hexdigest()
    (output/"manifest.json").write_text(json.dumps({
            "date":datetime.now(ZoneInfo("Asia/Singapore")).date().isoformat(),
            "created_utc":datetime.now(timezone.utc).isoformat(),**configuration,
            "files_sha256":hashes,"go_executables":{
                "current":os.environ.get("P4PERF_SDK_BINARY",str(ROOT/"bin/go-sdk")),
                "legacy":os.environ.get("P4PERF_LEGACY_BINARY",str(ROOT/"bin/go-legacy"))}},indent=2)+"\n")
