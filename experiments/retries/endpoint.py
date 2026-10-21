"""A stateful fault service and transparent BMv2 relay with binary RPC capture."""

import base64
import json
import threading

import grpc
from finsy.proto import p4r, p4r_grpc, rpc_status
from google.protobuf import any_pb2, json_format


def message(value):return json_format.MessageToDict(value,preserving_proto_field_name=True)
def binary(value):return base64.b64encode(value.SerializeToString()).decode()
def identity(entry):return int.from_bytes(entry.match[0].exact.value,'big')


class Endpoint(p4r_grpc.P4RuntimeServicer):
    def __init__(self,info,path,target=None):
        self.info=info;self.state={};self.records=[];self.file=path.open('w',buffering=1)
        self.lock=threading.Lock();self.token=None;self.attempt=0;self.case=None
        self.channel=grpc.insecure_channel(target) if target else None
        self.stub=p4r_grpc.P4RuntimeStub(self.channel) if target else None

    def configure(self,token,case):
        self.token=token;self.case=case;self.attempt=0;self.state={}

    def record(self,kind,request,code=0,status=None,responses=None):
        with self.lock:
            row=dict(sequence=len(self.records),token=self.token,kind=kind,rpc_code=code,
                     request=message(request),request_binary=binary(request))
            if status is not None:row['raw_status']=base64.b64encode(status).decode()
            if responses is not None:
                row['responses']=[message(r) for r in responses]
                row['response_binary']=[binary(r) for r in responses]
            self.records.append(row);self.file.write(json.dumps(row)+'\n')

    def Capabilities(self,request,context):return p4r.CapabilitiesResponse(p4runtime_api_version='1.3.0')
    def GetForwardingPipelineConfig(self,request,context):
        if self.stub:return self.stub.GetForwardingPipelineConfig(request,timeout=5)
        return p4r.GetForwardingPipelineConfigResponse(config=p4r.ForwardingPipelineConfig(p4info=self.info))

    def StreamChannel(self,iterator,context):
        if self.stub:
            call=self.stub.StreamChannel(iterator);context.add_callback(call.cancel)
            try:yield from call
            except grpc.RpcError as error:
                if context.is_active():context.abort(error.code(),error.details())
            finally:call.cancel()
        else:
            for request in iterator:
                if request.HasField('arbitration'):
                    response=p4r.StreamMessageResponse();response.arbitration.CopyFrom(request.arbitration)
                    response.arbitration.status.code=0;yield response

    def Write(self,request,context):
        if self.stub:
            try:
                reply=self.stub.Write(request,timeout=5);self.record('write',request);return reply
            except grpc.RpcError as error:
                trailers=error.trailing_metadata() or ()
                raw=next((v for k,v in trailers if k=='grpc-status-details-bin'),None)
                self.record('write',request,error.code().value[0],raw)
                context.set_trailing_metadata(trailers);context.abort(error.code(),error.details())
        assert request.atomicity==0
        codes=self.case['schedule'][self.attempt];assert len(codes)==len(request.updates),(codes,len(request.updates))
        for update,code in zip(request.updates,codes):
            if code==0:
                entry=update.entity.table_entry
                if update.type==p4r.Update.DELETE:self.state.pop(identity(entry),None)
                else:
                    clone=p4r.TableEntry();clone.CopyFrom(entry);self.state[identity(entry)]=clone
        first=self.attempt==0;self.attempt+=1
        response=self.case['response'] if first else 'normal'
        if not any(codes) and response!='transport':
            self.record('write',request);return p4r.WriteResponse()
        status=rpc_status.Status(code=14 if response=='transport' else 2,message='controlled batch outcome')
        if response not in ('missing','transport'):
            for code in codes:
                item=any_pb2.Any();item.Pack(p4r.Error(canonical_code=code,message='injected' if code else ''))
                status.details.append(item)
            if response=='short':
                status.details[0].CopyFrom(status.details[1]);del status.details[1:]
            if response=='wrong_type':status.details[1].Pack(p4r.TableEntry())
        raw=status.SerializeToString() if response not in ('missing','transport') else None
        self.record('write',request,status.code,raw)
        if raw is not None:context.set_trailing_metadata((('grpc-status-details-bin',raw),))
        context.abort(grpc.StatusCode.UNAVAILABLE if response=='transport' else grpc.StatusCode.UNKNOWN,status.message)

    def Read(self,request,context):
        if self.stub:responses=list(self.stub.Read(request,timeout=5))
        else:responses=[p4r.ReadResponse(entities=[p4r.Entity(table_entry=e) for e in self.state.values()])]
        self.record('read',request,responses=responses);yield from responses

    def snapshot(self):return [message(p4r.Entity(table_entry=e)) for e in self.state.values()]
    def close(self):
        if self.channel:self.channel.close()
        self.file.close()
