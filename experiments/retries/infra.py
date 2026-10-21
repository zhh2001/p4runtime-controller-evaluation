"""Owned BMv2 lifecycle, observer RPCs and the original E4 table fixture."""

import base64
import json
from pathlib import Path
import queue
import socket
import subprocess
import time

import grpc
from google.protobuf import text_format
from finsy.proto import p4i, p4r, p4r_grpc, rpc_status

ROOT = Path(__file__).resolve().parent
INFO = text_format.Parse((ROOT / 'fixtures/keys.p4info.txt').read_text(), p4i.P4Info())
CONFIG = (ROOT / 'fixtures/keys.json').read_bytes()


def status_record(error):
    if error is None:
        return {"rpc_code":0,"details":[],"trailing_metadata":[]}
    result={"rpc_code":error.code().value[0],"error":error.details(),"details":[],"trailing_metadata":[]}
    for key,value in error.trailing_metadata() or ():
        result["trailing_metadata"].append({"key":key,"value":base64.b64encode(value).decode() if isinstance(value,bytes) else value,"binary":isinstance(value,bytes)})
        if key=="grpc-status-details-bin":
            status=rpc_status.Status.FromString(value)
            for index,detail in enumerate(status.details):
                item=p4r.Error()
                if detail.Unpack(item):
                    result["details"].append({"index":index,"code":item.canonical_code,"message":item.message,"space":item.space,"vendor_code":item.code})
    return result


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class Target:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.directory.mkdir(exist_ok=True,parents=True)
        self.port=unused_port()
        self.thrift=unused_port()
        while self.thrift==self.port:self.thrift=unused_port()
        self.address=f"127.0.0.1:{self.port}"
        self.process=None
        self.generation=0
        self.history=[]

    def start(self):
        assert self.process is None
        self.generation+=1
        self.ipc=Path(f"/tmp/p4runtime-retries-{id(self)}-{self.generation}.ipc")
        command=["simple_switch_grpc","--no-p4","--device-id","1","--thrift-port",str(self.thrift),"--notifications-addr","ipc://"+str(self.ipc),"--log-console","-L","info","--","--grpc-server-addr",self.address,"--cpu-port","255"]
        self.log=open(self.directory/f"target-{self.generation}.log","w")
        self.process=subprocess.Popen(command,stdout=self.log,stderr=subprocess.STDOUT)
        self.history.append({"generation":self.generation,"pid":self.process.pid,"command":command,"start_ns":time.monotonic_ns()})
        channel=grpc.insecure_channel(self.address)
        try:
            grpc.channel_ready_future(channel).result(timeout=10)
            if self.process.poll() is not None:raise RuntimeError("owned BMv2 process exited")
        except BaseException:
            self.stop()
            raise
        finally:channel.close()

    def stop(self):
        if self.process is None:return
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=5)
        self.history[-1].update(stop_ns=time.monotonic_ns(),exit_code=self.process.returncode)
        self.process=None
        self.log.close()
        self.ipc.unlink(missing_ok=True)


class Session:
    def __init__(self,address,arbitrate=False):
        self.channel=grpc.insecure_channel(address)
        self.stub=p4r_grpc.P4RuntimeStub(self.channel)
        self.call=None
        if arbitrate:
            self.queue=queue.Queue()
            def requests():
                while True:
                    item=self.queue.get()
                    if item is None:return
                    yield item
            self.call=self.stub.StreamChannel(requests(),timeout=60)
            self.queue.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1))))
            try:
                response=next(self.call)
                if response.arbitration.status.code!=0:raise RuntimeError("application arbitration was rejected")
            except BaseException:
                self.close()
                raise

    def install(self):
        return self.stub.SetForwardingPipelineConfig(p4r.SetForwardingPipelineConfigRequest(device_id=1,election_id=p4r.Uint128(low=1),action=p4r.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,config=p4r.ForwardingPipelineConfig(p4info=INFO,p4_device_config=CONFIG)),timeout=5)

    def read(self):
        request=p4r.ReadRequest(device_id=1,entities=[p4r.Entity(table_entry=p4r.TableEntry())])
        return [entity.table_entry for response in self.stub.Read(request,timeout=5) for entity in response.entities if entity.HasField("table_entry") and not entity.table_entry.is_default_action]

    def pipeline(self):
        return self.stub.GetForwardingPipelineConfig(p4r.GetForwardingPipelineConfigRequest(device_id=1),timeout=5)

    def write(self,updates,atomicity=0):
        request=p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=updates,atomicity=atomicity)
        try:self.stub.Write(request,timeout=5);return status_record(None)
        except grpc.RpcError as error:return status_record(error)

    def close(self):
        if self.call is not None:
            self.queue.put(None);self.call.cancel()
        self.channel.close()


def entry(key,marker=b"seed"):
    table=next(table for table in INFO.tables if table.preamble.name=="MyIngress.t_exact")
    action=next(action for action in INFO.actions if action.preamble.name=="MyIngress.forward")
    return p4r.TableEntry(table_id=table.preamble.id,match=[p4r.FieldMatch(field_id=table.match_fields[0].id,exact=p4r.FieldMatch.Exact(value=key.to_bytes(max(1,(key.bit_length()+7)//8),"big")))],action=p4r.TableAction(action=p4r.Action(action_id=action.preamble.id,params=[p4r.Action.Param(param_id=action.params[0].id,value=b"\x01")])),metadata=marker)
