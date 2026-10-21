from concurrent.futures import ThreadPoolExecutor
import hashlib
import queue
import threading
import time

import grpc
from google.protobuf import text_format
from finsy.proto import p4i, p4r, p4r_grpc

from common import ROOT, message

INFO = text_format.Parse((ROOT / "fixtures/stream.p4info.txt").read_text(), p4i.P4Info())


class Stream:
    def __init__(self, number, context):
        self.number, self.context = number, context
        self.responses = queue.Queue()
        self.eof = threading.Event()
        self.closed = threading.Event()


class Service(p4r_grpc.P4RuntimeServicer):
    def __init__(self, trace, paused=False, hold_final=False):
        self.trace = trace
        self.closing = False
        self.read_gate = threading.Event()
        if not paused:
            self.read_gate.set()
        self.final_gate = threading.Event()
        if not hold_final:
            self.final_gate.set()
        self.streams = {}
        self.lock = threading.Lock()
        self.threads = []
        self.server = grpc.server(ThreadPoolExecutor(max_workers=16),
                                  options=[("grpc.max_receive_message_length", 4 * 1024 * 1024)])
        p4r_grpc.add_P4RuntimeServicer_to_server(self, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.address = f"127.0.0.1:{port}"
        self.server.start()

    def Capabilities(self, request, context):
        return p4r.CapabilitiesResponse(p4runtime_api_version="1.3.0")

    def GetForwardingPipelineConfig(self, request, context):
        return p4r.GetForwardingPipelineConfigResponse(
            config=p4r.ForwardingPipelineConfig(p4info=INFO))

    def StreamChannel(self, iterator, context):
        with self.lock:
            number = len(self.streams) + 1
            stream = Stream(number, context)
            self.streams[number] = stream
        self.trace.add("stream_open", stream_id=number)
        try:
            request = next(iterator)
            assert request.HasField("arbitration")
            self.trace.add("arbitration_received", stream_id=number, request=message(request))
            response = p4r.StreamMessageResponse(arbitration=p4r.MasterArbitrationUpdate(
                device_id=1, election_id=p4r.Uint128(low=1), status={"code": 0}))
            self.trace.add("arbitration_emit", stream_id=number, code=0)
            yield response
            def receive():
                try:
                    while context.is_active():
                        while not self.read_gate.wait(.01):
                            if not context.is_active():
                                return
                        request = next(iterator)
                        if request.HasField("packet"):
                            payload = request.packet.payload
                            self.trace.add("packet_out_received", stream_id=number,
                                           packet_id=int.from_bytes(payload[8:16], "big"),
                                           size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                                           metadata=[message(item) for item in request.packet.metadata])
                        else:
                            self.trace.add("stream_request_received", stream_id=number, request=message(request))
                except StopIteration:
                    self.trace.add("request_eof", stream_id=number)
                    stream.eof.set()
                    stream.responses.put(None)
                except grpc.RpcError:
                    self.trace.add("request_cancelled", stream_id=number)
            thread = threading.Thread(target=receive, daemon=True)
            self.threads.append(thread)
            thread.start()
            while context.is_active():
                try:
                    response = stream.responses.get(timeout=.02)
                except queue.Empty:
                    continue
                if response is None:
                    while not self.final_gate.wait(.01):
                        if self.closing or not context.is_active():
                            return
                    if self.closing or not context.is_active():
                        return
                    self.trace.add("stream_final_ok", stream_id=number)
                    return
                if response.HasField("packet"):
                    self.trace.add("packet_in_emit", stream_id=number,
                                   packet_id=int.from_bytes(response.packet.payload[8:16], "big"))
                elif response.HasField("arbitration"):
                    self.trace.add("arbitration_emit", stream_id=number,
                                   code=response.arbitration.status.code)
                yield response
        finally:
            self.trace.add("stream_closed", stream_id=number, active=context.is_active())
            stream.closed.set()

    def send_packets(self, count, rate):
        stream = self.streams[max(self.streams)]
        started = time.monotonic()
        for number in range(count):
            payload = b"P4EXP001" + number.to_bytes(8, "big") + bytes(48)
            stream.responses.put(p4r.StreamMessageResponse(packet=p4r.PacketIn(payload=payload)))
            deadline = started + (number + 1) / rate
            if deadline > time.monotonic():
                time.sleep(deadline - time.monotonic())
        stream.responses.put(p4r.StreamMessageResponse(arbitration=p4r.MasterArbitrationUpdate(
            device_id=1, election_id=p4r.Uint128(low=2), status={"code": 6})))

    def close(self):
        self.closing = True
        self.server.stop(0).wait(timeout=5)
        self.read_gate.set()
        self.final_gate.set()
        for stream in self.streams.values():
            assert stream.closed.wait(timeout=2), 'server stream did not finish'
        for thread in self.threads:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in self.threads)
