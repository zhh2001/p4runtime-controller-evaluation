"""Controlled P4Runtime endpoint for the finite E1/E4 pilot corpus."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import threading
import time

import grpc
from google.protobuf import any_pb2, json_format, text_format, wrappers_pb2
from finsy.proto import p4i, p4r, p4r_grpc, rpc_status


def number(data, width):
    if not data or int.from_bytes(data, "big").bit_length() > width:
        raise ValueError("value exceeds declared width or is empty")
    return int.from_bytes(data, "big")


def validate_entry(info, entry):
    """Check the subset exercised by this corpus without SDK encoder calls."""
    tables = {t.preamble.id: t for t in info.tables}
    actions = {a.preamble.id: a for a in info.actions}
    if entry.table_id not in tables:
        raise ValueError("unknown table ID")
    table = tables[entry.table_id]
    fields = {f.id: f for f in table.match_fields}
    seen = set()
    needs_priority = any(f.match_type in (p4i.MatchField.TERNARY, p4i.MatchField.RANGE, p4i.MatchField.OPTIONAL) for f in fields.values())
    if (needs_priority and entry.priority <= 0) or (not needs_priority and entry.priority != 0):
        raise ValueError("invalid priority for table")
    if entry.is_default_action:
        raise ValueError("default entries are outside this pilot")
    for match in entry.match:
        if match.field_id not in fields or match.field_id in seen:
            raise ValueError("unknown or duplicate match field")
        seen.add(match.field_id)
        field = fields[match.field_id]
        kind = match.WhichOneof("field_match_type")
        expected = p4i.MatchField.MatchType.Name(field.match_type).lower()
        if kind != expected:
            raise ValueError("match type differs from declaration")
        part = getattr(match, kind)
        if kind in ("exact", "optional"):
            number(part.value, field.bitwidth)
        elif kind == "lpm":
            value = number(part.value, field.bitwidth)
            if not 0 < part.prefix_len <= field.bitwidth:
                raise ValueError("invalid LPM prefix")
            if value & ((1 << (field.bitwidth - part.prefix_len)) - 1):
                raise ValueError("LPM host bits are set")
        elif kind == "ternary":
            value = number(part.value, field.bitwidth)
            mask = number(part.mask, field.bitwidth)
            if not mask or value & mask != value:
                raise ValueError("invalid ternary value or wildcard")
        elif kind == "range":
            low, high = number(part.low, field.bitwidth), number(part.high, field.bitwidth)
            if low > high or (low == 0 and high == (1 << field.bitwidth) - 1):
                raise ValueError("invalid range or explicit full-range wildcard")
    if any(f.match_type == p4i.MatchField.EXACT and f.id not in seen for f in fields.values()):
        raise ValueError("missing EXACT field")
    if entry.action.WhichOneof("type") != "action":
        raise ValueError("missing direct action")
    action = entry.action.action
    refs = {a.id for a in table.action_refs}
    if action.action_id not in actions or action.action_id not in refs:
        raise ValueError("unknown or disallowed action")
    params = {p.id: p for p in actions[action.action_id].params}
    seen_params = set()
    for param in action.params:
        if param.param_id not in params or param.param_id in seen_params:
            raise ValueError("unknown or duplicate action parameter")
        seen_params.add(param.param_id)
        number(param.value, params[param.param_id].bitwidth)
    if seen_params != set(params):
        raise ValueError("missing action parameter")


class Endpoint(p4r_grpc.P4RuntimeServicer):
    def __init__(self, info, cases, log):
        self.info = info
        self.cases = {c["id"]: c for c in cases if c["suite"] == "E4"}
        self.log = log
        self.lock = threading.Lock()

    def Capabilities(self, request, context):
        return p4r.CapabilitiesResponse(p4runtime_api_version="1.3.0")

    def GetForwardingPipelineConfig(self, request, context):
        return p4r.GetForwardingPipelineConfigResponse(config=p4r.ForwardingPipelineConfig(p4info=self.info))

    def SetForwardingPipelineConfig(self, request, context):
        return p4r.SetForwardingPipelineConfigResponse()

    def StreamChannel(self, iterator, context):
        for request in iterator:
            if request.HasField("arbitration"):
                response = p4r.StreamMessageResponse()
                response.arbitration.CopyFrom(request.arbitration)
                response.arbitration.status.code = 0
                yield response

    def Read(self, request, context):
        yield p4r.ReadResponse()

    def Write(self, request, context):
        if not request.updates:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "empty pilot request")
        marker = request.updates[0].entity.table_entry.metadata.decode()
        suite, case_id, adapter = marker.split("|", 2)
        errors = []
        if suite == "E1":
            for update in request.updates:
                try:
                    validate_entry(self.info, update.entity.table_entry)
                    errors.append(p4r.Error())
                except ValueError as exc:
                    errors.append(p4r.Error(canonical_code=3, message=str(exc)))
            fault = "none"
        else:
            case = self.cases[case_id]
            errors = [p4r.Error(canonical_code=code, message="" if code == 0 else f"injected index {i}") for i, code in enumerate(case["codes"])]
            fault = case["fault"]
        row = {"monotonic_ns": time.monotonic_ns(), "suite": suite, "case_id": case_id,
               "adapter": adapter, "codes": [e.canonical_code for e in errors],
               "fault": fault, "request": json_format.MessageToDict(request, preserving_proto_field_name=True)}
        with self.lock:
            self.log.write(json.dumps(row) + "\n")
            self.log.flush()
        if all(e.canonical_code == 0 for e in errors):
            return p4r.WriteResponse()
        status = rpc_status.Status(code=2, message="controlled write outcome")
        if fault != "missing":
            for i, error in enumerate(errors):
                detail = any_pb2.Any()
                detail.Pack(wrappers_pb2.StringValue(value="wrong message type") if fault == "type" and i == 0 else error)
                status.details.append(detail)
        context.set_trailing_metadata((("grpc-status-details-bin", status.SerializeToString()),))
        context.abort(grpc.StatusCode.UNKNOWN, status.message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--p4info", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    info = text_format.Parse(Path(args.p4info).read_text(), p4i.P4Info())
    with open(args.log, "w") as log:
        server = grpc.server(ThreadPoolExecutor(max_workers=8))
        p4r_grpc.add_P4RuntimeServicer_to_server(Endpoint(info, json.loads(Path(args.cases).read_text()), log), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        signal.signal(signal.SIGTERM, lambda *_: server.stop(0))
        print(json.dumps({"address": f"127.0.0.1:{port}"}), flush=True)
        server.wait_for_termination()


if __name__ == "__main__":
    main()
