"""Controlled service and request-recording relay for E1 boundaries."""

import json
import threading
import time

import grpc
from finsy.proto import p4r, p4r_grpc, rpc_status
from google.protobuf import any_pb2, json_format

from oracle import Invalid, validate


def message(value):
    return json_format.MessageToDict(value, preserving_proto_field_name=True)


class Endpoint(p4r_grpc.P4RuntimeServicer):
    def __init__(self, info, output, target=None):
        self.info = info
        self.output = output
        self.file = output.open("w", buffering=1)
        self.records = []
        self.token = None
        self.lock = threading.Lock()
        self.state = {}
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
        if self.stub:
            try:
                response = self.stub.Write(request, timeout=5)
                self.record("write", request)
                return response
            except grpc.RpcError as error:
                trailing = error.trailing_metadata() or ()
                details = []
                for key, value in trailing:
                    if key == "grpc-status-details-bin":
                        status = rpc_status.Status.FromString(value)
                        for detail in status.details:
                            item = p4r.Error()
                            if detail.Unpack(item): details.append(item.canonical_code)
                self.record("write", request, error.code().value[0], details=details)
                context.set_trailing_metadata(trailing)
                context.abort(error.code(), error.details())
        try:
            if len(request.updates) != 1 or request.updates[0].type != p4r.Update.MODIFY:
                raise Invalid("expected one MODIFY")
            entity = request.updates[0].entity
            semantics = validate(self.info, entity, "write")
        except Invalid as error:
            status = rpc_status.Status(code=2, message="controlled boundary rejection")
            detail = any_pb2.Any()
            detail.Pack(p4r.Error(canonical_code=error.code, message=str(error)))
            status.details.append(detail)
            self.record("write", request, 2, details=[error.code])
            context.set_trailing_metadata((("grpc-status-details-bin", status.SerializeToString()),))
            context.abort(grpc.StatusCode.UNKNOWN, status.message)
        clone = p4r.Entity()
        clone.CopyFrom(entity)
        state_key = semantics["kind"], semantics["id"], semantics.get("index", semantics.get("key"))
        self.state[state_key] = clone
        self.record("write", request, semantics=semantics)
        return p4r.WriteResponse()

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
        try:
            if len(request.entities) != 1:
                raise Invalid("expected one selector")
            entity = request.entities[0]
            semantics = validate(self.info, entity, "read")
        except Invalid as error:
            self.record("read", request, error.code)
            context.abort(grpc.StatusCode.OUT_OF_RANGE if error.code == 11 else grpc.StatusCode.INVALID_ARGUMENT, str(error))
        kind = semantics["kind"]
        entry = getattr(entity, kind)
        base = kind.removesuffix("_entry")
        declarations = getattr(self.info, {"counter": "counters", "meter": "meters", "register": "registers"}[base])
        declaration = next(v for v in declarations if v.preamble.id == semantics["id"])
        indexes = ([10000, 10001] if declaration.HasField("index_type_name") else list(range(declaration.size))) if semantics["index"] == "all" else [semantics["index"]]
        response = p4r.ReadResponse()
        for index in indexes:
            key = kind, semantics["id"], index
            if key in self.state:
                response.entities.add().CopyFrom(self.state[key])
            else:
                result = response.entities.add()
                getattr(result, kind).CopyFrom(entry)
                getattr(result, kind).index.index = index
                if kind == "register_entry": getattr(result, kind).data.bitstring = b"\x00"
                elif kind == "counter_entry": getattr(result, kind).data.SetInParent()
                else: getattr(result, kind).config.SetInParent()
        self.record("read", request, semantics=semantics, responses=[response])
        yield response

    def close(self):
        self.file.close()
        if self.channel: self.channel.close()
