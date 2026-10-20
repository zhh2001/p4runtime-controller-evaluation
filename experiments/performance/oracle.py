from concurrent.futures import ThreadPoolExecutor
import threading

import grpc
from finsy.proto import p4r,p4r_grpc

from common import ACTION,INFO,MARKER,TABLE,decode_entries,entry,message


class Oracle(p4r_grpc.P4RuntimeServicer):
    def __init__(self,trace):
        self.trace=trace
        self.lock=threading.Lock()
        self.state={}
        self.fail_modify=False
        self.server=grpc.server(ThreadPoolExecutor(max_workers=32))
        p4r_grpc.add_P4RuntimeServicer_to_server(self,self.server)
        port=self.server.add_insecure_port("127.0.0.1:0")
        self.address=f"127.0.0.1:{port}"
        self.server.start()

    def Capabilities(self,request,context):
        return p4r.CapabilitiesResponse(p4runtime_api_version="1.3.0")

    def GetForwardingPipelineConfig(self,request,context):
        return p4r.GetForwardingPipelineConfigResponse(config=p4r.ForwardingPipelineConfig(p4info=INFO))

    def SetForwardingPipelineConfig(self,request,context):
        with self.lock:self.state={}
        return p4r.SetForwardingPipelineConfigResponse()

    def StreamChannel(self,iterator,context):
        for request in iterator:
            assert request.HasField("arbitration")
            yield p4r.StreamMessageResponse(arbitration=p4r.MasterArbitrationUpdate(
                device_id=1,election_id=p4r.Uint128(low=1),status={"code":0}))

    def Write(self,request,context):
        try:
            assert request.device_id==1 and request.election_id.low==1
            assert request.updates and request.atomicity==0
            with self.lock:
                items=[update.entity.table_entry for update in request.updates]
                decoded=decode_entries(items)
                assert list(decoded)==list(range(min(decoded),max(decoded)+1))
                kind=request.updates[0].type
                assert all(update.type==kind for update in request.updates)
                event=self.trace.add("write",request=message(request))
                if kind==p4r.Update.MODIFY and self.fail_modify:
                    self.fail_modify=False
                    self.trace.add("injected_unavailable",request_sequence=event["sequence"],applied_updates=0)
                    context.abort(grpc.StatusCode.UNAVAILABLE,"injected before applying updates")
                for key,port in decoded.items():
                    if kind==p4r.Update.INSERT:assert key not in self.state
                    else:assert kind==p4r.Update.MODIFY and key in self.state
                    self.state[key]=port
            return p4r.WriteResponse()
        except AssertionError as error:
            self.trace.add("oracle_rejected",request=message(request),error=str(error))
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,"request does not match fixture semantics")

    def Read(self,request,context):
        assert request.device_id==1 and len(request.entities)==1
        selector=request.entities[0].table_entry
        assert selector.table_id==TABLE.preamble.id
        assert not selector.HasField("action") and not selector.is_default_action
        with self.lock:
            if selector.match:
                assert len(selector.match)==1 and selector.match[0].field_id==TABLE.match_fields[0].id
                assert selector.match[0].WhichOneof("field_match_type")=="exact"
                key=int.from_bytes(selector.match[0].exact.value,"big")
                values={key:self.state[key]} if key in self.state else {}
            else:values=dict(self.state)
        self.trace.add("read",request=message(request),count=len(values))
        items=[p4r.Entity(table_entry=entry(key,port)) for key,port in sorted(values.items())]
        for start in range(0,len(items),256):
            yield p4r.ReadResponse(entities=items[start:start+256])

    def close(self):
        self.server.stop(0).wait(timeout=5)
