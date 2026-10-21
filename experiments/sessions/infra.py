from concurrent.futures import ThreadPoolExecutor
import base64
import json
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time

import grpc
from google.protobuf import json_format, text_format
from finsy.proto import p4i, p4r, p4r_grpc, rpc_status

ROOT=Path(__file__).resolve().parent
INFO=text_format.Parse((ROOT/"fixtures/keys.p4info.txt").read_text(),p4i.P4Info())
CONFIG=(ROOT/"fixtures/keys.json").read_bytes()


def message(value):
    return json_format.MessageToDict(value,preserving_proto_field_name=True)


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


class Trace:
    def __init__(self,path):
        self.file=open(path,"w")
        self.rows=[]
        self.condition=threading.Condition()

    def add(self,event,**fields):
        with self.condition:
            row={"sequence":len(self.rows),"monotonic_ns":time.monotonic_ns(),"event":event,**fields}
            self.rows.append(row)
            self.file.write(json.dumps(row)+"\n")
            self.file.flush()
            self.condition.notify_all()
            return row

    def wait(self,predicate,timeout=60):
        deadline=time.monotonic()+timeout
        with self.condition:
            while True:
                found=next((row for row in self.rows if predicate(row)),None)
                if found is not None:return found
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError("trace event was not observed")
                self.condition.wait(min(remaining,0.1))

    def close(self):self.file.close()


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
        self.ipc=Path(f"/tmp/p4runtime-sessions-{id(self)}-{self.generation}.ipc")
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


class Relay(p4r_grpc.P4RuntimeServicer):
    def __init__(self,target,trace):
        self.trace=trace
        self.channel=grpc.insecure_channel(target)
        self.stub=p4r_grpc.P4RuntimeStub(self.channel)
        self.lock=threading.Lock()
        self.stream_counter=0
        self.calls={}
        self.cuts={}
        self.gate=threading.Event();self.gate.set()
        self.server=grpc.server(ThreadPoolExecutor(max_workers=24))
        p4r_grpc.add_P4RuntimeServicer_to_server(self,self.server)
        self.port=self.server.add_insecure_port("127.0.0.1:0")
        self.address=f"127.0.0.1:{self.port}"
        self.server.start()

    def unary(self,name,request,context):
        try:return getattr(self.stub,name)(request,timeout=5)
        except grpc.RpcError as error:
            context.set_trailing_metadata(error.trailing_metadata() or ())
            context.abort(error.code(),error.details())

    def Capabilities(self,request,context):return self.unary("Capabilities",request,context)
    def GetForwardingPipelineConfig(self,request,context):return self.unary("GetForwardingPipelineConfig",request,context)
    def SetForwardingPipelineConfig(self,request,context):return self.unary("SetForwardingPipelineConfig",request,context)

    def Write(self,request,context):
        start=self.trace.add("write_start",request=message(request))
        try:
            result=self.stub.Write(request,timeout=5)
        except grpc.RpcError as error:
            self.trace.add("write_end",request_sequence=start["sequence"],request=message(request),**status_record(error))
            context.set_trailing_metadata(error.trailing_metadata() or ())
            context.abort(error.code(),error.details())
        self.trace.add("write_end",request_sequence=start["sequence"],request=message(request),**status_record(None))
        return result

    def Read(self,request,context):
        try:yield from self.stub.Read(request,timeout=5)
        except grpc.RpcError as error:
            context.set_trailing_metadata(error.trailing_metadata() or ())
            context.abort(error.code(),error.details())

    def StreamChannel(self,iterator,context):
        with self.lock:
            self.stream_counter+=1
            stream_id=self.stream_counter
        self.trace.add("stream_open",stream_id=stream_id)
        def requests():
            for request in iterator:
                if request.HasField("arbitration"):
                    self.trace.add("arbitration_received",stream_id=stream_id,request=message(request))
                    while not self.gate.wait(0.02):
                        if not context.is_active():return
                    self.trace.add("arbitration_forwarded",stream_id=stream_id,request=message(request))
                yield request
        call=self.stub.StreamChannel(requests())
        with self.lock:self.calls[stream_id]=call
        context.add_callback(call.cancel)
        try:
            for response in call:
                if response.HasField("arbitration"):
                    self.trace.add("arbitration_response",stream_id=stream_id,response=message(response))
                yield response
        except grpc.RpcError as error:
            with self.lock:mode=self.cuts.get(stream_id)
            self.trace.add("stream_end",stream_id=stream_id,mode=mode,**status_record(error))
            if context.is_active():
                if mode=="eof":return
                if mode=="unavailable":context.abort(grpc.StatusCode.UNAVAILABLE,"injected stream termination")
                context.set_trailing_metadata(error.trailing_metadata() or ())
                context.abort(error.code(),error.details())
        finally:
            with self.lock:self.calls.pop(stream_id,None)

    def cut(self,stream_id,mode):
        self.gate.clear()
        with self.lock:
            self.cuts[stream_id]=mode
            call=self.calls[stream_id]
        event=self.trace.add("fault_injected",stream_id=stream_id,mode=mode)
        call.cancel()
        return event

    def is_active(self,stream_id):
        with self.lock:
            call=self.calls.get(stream_id)
            return call is not None and not call.done()

    def close(self):
        self.gate.set()
        self.server.stop(0).wait(timeout=5)
        self.channel.close()


class Session:
    def __init__(self,address,arbitrate=False):
        self.channel=grpc.insecure_channel(address)
        self.stub=p4r_grpc.P4RuntimeStub(self.channel)
        self.call=None
        self.election=p4r.Uint128(low=1)
        if arbitrate:
            self.queue=queue.Queue()
            def requests():
                while True:
                    item=self.queue.get()
                    if item is None:return
                    yield item
            self.call=self.stub.StreamChannel(requests(),timeout=60)
            self.queue.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1))))
            response=next(self.call)
            if response.arbitration.status.code!=0:raise RuntimeError("application arbitration was rejected")

    def install(self):
        return self.stub.SetForwardingPipelineConfig(p4r.SetForwardingPipelineConfigRequest(device_id=1,election_id=self.election,action=p4r.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,config=p4r.ForwardingPipelineConfig(p4info=INFO,p4_device_config=CONFIG)),timeout=5)

    def read(self):
        request=p4r.ReadRequest(device_id=1,entities=[p4r.Entity(table_entry=p4r.TableEntry())])
        return [entity.table_entry for response in self.stub.Read(request,timeout=5) for entity in response.entities if entity.HasField("table_entry") and not entity.table_entry.is_default_action]

    def pipeline(self):
        return self.stub.GetForwardingPipelineConfig(p4r.GetForwardingPipelineConfigRequest(device_id=1),timeout=5)

    def write(self,updates,atomicity=0):
        request=p4r.WriteRequest(device_id=1,election_id=self.election,updates=updates,atomicity=atomicity)
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


def update(key,marker=b"seed"):
    return p4r.Update(type=p4r.Update.INSERT,entity=p4r.Entity(table_entry=entry(key,marker)))
