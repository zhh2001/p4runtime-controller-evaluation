"""Controlled service and request-recording relay for E1 boundaries."""

import json
import threading
import time

import grpc
from finsy.proto import p4r, p4r_grpc, rpc_status
from google.protobuf import any_pb2, json_format

from oracle import Invalid, validate
from model import initial_entries, key, lookup, default_entry


def message(value):
    return json_format.MessageToDict(value, preserving_proto_field_name=True)


class Endpoint(p4r_grpc.P4RuntimeServicer):
    def __init__(self, info, output, target=None):
        self.info = info
        self.output = output
        self.file = output.open("w", buffering=1)
        self.records = []
        self.failures = []
        self.token = None
        self.lock = threading.Lock()
        self.state = {key(e):e for e in initial_entries(info)}
        self.channel = grpc.insecure_channel(target) if target else None
        self.stub = p4r_grpc.P4RuntimeStub(self.channel) if target else None

    def record(self, operation, request, code=0, semantics=None, responses=None, details=None):
        with self.lock:
            row = dict(sequence=len(self.records), monotonic_ns=time.monotonic_ns(), token=self.token,
                       operation=operation, request=message(request), rpc_code=code, semantics=semantics,
                       responses=[message(v) for v in responses or []], details=details)
            self.records.append(row)
            self.file.write(json.dumps(row) + "\n")

    def Capabilities(self, request, context):
        return p4r.CapabilitiesResponse(p4runtime_api_version="1.3.0")

    def GetForwardingPipelineConfig(self, request, context):
        return p4r.GetForwardingPipelineConfigResponse(config=p4r.ForwardingPipelineConfig(p4info=self.info))

    def StreamChannel(self, iterator, context):
        if self.stub:
            call = self.stub.StreamChannel(iterator)
            context.add_callback(call.cancel)
            try:
                yield from call
            finally:
                call.cancel()
            return
        for request in iterator:
            if request.HasField("arbitration"):
                response = p4r.StreamMessageResponse()
                response.arbitration.CopyFrom(request.arbitration)
                response.arbitration.status.code = 0
                yield response

    def Write(self, request, context):
        count=len(self.records)
        try:return self._write(request,context)
        except Exception as error:
            if len(self.records)==count:
                self.failures.append(dict(token=self.token,error=repr(error)))
                self.record('harness_failure',request,2,details=[repr(error)])
            raise

    def _write(self, request, context):
        if self.stub:
            try:
                response = self.stub.Write(request, timeout=5)
                self.record("write", request)
                return response
            except grpc.RpcError as error:
                trailing = error.trailing_metadata() or ()
                details = []
                for header, value in trailing:
                    if header == "grpc-status-details-bin":
                        status = rpc_status.Status.FromString(value)
                        for detail in status.details:
                            item = p4r.Error()
                            if detail.Unpack(item): details.append(item.canonical_code)
                self.record("write", request, error.code().value[0], details=details)
                context.set_trailing_metadata(trailing)
                context.abort(error.code(), error.details())
        try:
            if len(request.updates) != 1:
                raise Invalid("expected one update")
            update=request.updates[0]
            if update.entity.WhichOneof('entity')!='table_entry':raise Invalid('expected table entry')
            entry=update.entity.table_entry
            operation=p4r.Update.Type.Name(update.type)
            semantics=validate(self.info,entry,operation,self.state)
            identity=key(entry)
        except Invalid as error:
            status = rpc_status.Status(code=2, message="controlled table rejection")
            detail = any_pb2.Any()
            detail.Pack(p4r.Error(canonical_code=error.code, message=str(error)))
            status.details.append(detail)
            self.record("write", request, 2, details=[error.code])
            context.set_trailing_metadata((("grpc-status-details-bin", status.SerializeToString()),))
            context.abort(grpc.StatusCode.UNKNOWN, status.message)
        if operation=='DELETE':del self.state[identity]
        else:
            clone=p4r.TableEntry();clone.CopyFrom(entry)
            if entry.is_default_action and not entry.HasField('action'):
                clone.CopyFrom(default_entry(self.info,next(t for t in self.info.tables if t.preamble.id==entry.table_id)))
            self.state[identity]=clone
        self.record("write", request, semantics=semantics)
        return p4r.WriteResponse()

    def reset(self,case=None):
        self.state={key(e):e for e in initial_entries(self.info)}
        if case and case['category']=='valid' and case['default'] and case['action_kind']=='none':
            table=lookup(self.info,'tables',case['table'])
            entry=default_entry(self.info,table)
            entry.action.action.params[0].value=b'\x07'
            self.state[key(entry)]=entry

    def snapshot(self):
        return sorted([message(p4r.Entity(table_entry=e)) for e in self.state.values()],key=lambda e:json.dumps(e,sort_keys=True))

    def Read(self, request, context):
        if self.stub:
            try:
                responses = list(self.stub.Read(request, timeout=5))
                self.record("read", request, responses=responses)
                yield from responses
                return
            except grpc.RpcError as error:
                self.record("read", request, error.code().value[0])
                context.set_trailing_metadata(error.trailing_metadata() or ())
                context.abort(error.code(), error.details())
        raise RuntimeError('controlled Read is not used in this write-only corpus')

    def close(self):
        self.file.close()
        if self.channel: self.channel.close()
