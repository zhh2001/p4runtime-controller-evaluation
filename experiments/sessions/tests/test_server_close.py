from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import grpc
from finsy.proto import p4r,p4r_grpc
from common import Trace
from server import Service


class ServerCloseTests(unittest.TestCase):
    def test_close_does_not_release_held_final_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'rpc.jsonl');service=Service(trace,hold_final=True)
            channel=grpc.insecure_channel(service.address);stub=p4r_grpc.P4RuntimeStub(channel)
            requests=queue.Queue()
            def iterator():
                while (v:=requests.get()) is not None:yield v
            call=stub.StreamChannel(iterator())
            try:
                requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1))))
                self.assertEqual(next(call).arbitration.status.code,0)
                requests.put(None)
                trace.wait(lambda v:v['event']=='request_eof',2)
                self.assertFalse(service.final_gate.is_set())
                service.close()
                self.assertFalse(any(v['event']=='stream_final_ok' for v in trace.rows))
                self.assertTrue(all(s.closed.is_set() for s in service.streams.values()))
            finally:call.cancel();channel.close();service.close();trace.close()

    def test_explicit_final_release_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'rpc.jsonl');service=Service(trace,hold_final=True)
            channel=grpc.insecure_channel(service.address);stub=p4r_grpc.P4RuntimeStub(channel)
            request=p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1)))
            call=stub.StreamChannel(iter([request]))
            try:
                self.assertEqual(next(call).arbitration.status.code,0)
                trace.wait(lambda v:v['event']=='request_eof',2)
                service.final_gate.set()
                self.assertEqual(list(call),[])
                self.assertEqual(sum(v['event']=='stream_final_ok' for v in trace.rows),1)
            finally:call.cancel();channel.close();service.close();trace.close()


if __name__=='__main__':unittest.main()
