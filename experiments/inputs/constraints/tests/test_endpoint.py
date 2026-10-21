import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import grpc
from finsy.proto import p4i,p4r,p4r_grpc
from google.protobuf import text_format
from endpoint import Endpoint
from fixtures import ROOT
from model import raw_entry


class ControlledEndpointTests(unittest.TestCase):
    def test_rpc_success_rejection_delete_and_reset(self):
        info=text_format.Parse((ROOT/'fixtures/target.p4info.txt').read_text(),p4i.P4Info())
        cases={c['id']:c for c in json.loads((ROOT/'fixtures/cases.json').read_text())}
        with tempfile.TemporaryDirectory() as tmp:
            endpoint=Endpoint(info,Path(tmp)/'rpc.jsonl')
            server=grpc.server(ThreadPoolExecutor(max_workers=2));p4r_grpc.add_P4RuntimeServicer_to_server(endpoint,server)
            port=server.add_insecure_port('127.0.0.1:0');server.start()
            channel=grpc.insecure_channel(f'127.0.0.1:{port}');stub=p4r_grpc.P4RuntimeStub(channel)
            try:
                for name in ('ordinary-modify','default-reset','ordinary-delete','constant-entry-modify'):
                    endpoint.reset();endpoint.token=name;c=cases[name]
                    req=p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=[p4r.Update(type=getattr(p4r.Update,c['update']),entity=p4r.Entity(table_entry=raw_entry(info,c)))])
                    if c['category']=='invalid':
                        with self.assertRaises(grpc.RpcError):stub.Write(req,timeout=2)
                    else:stub.Write(req,timeout=2)
                    self.assertEqual(endpoint.records[-1]['token'],name)
                self.assertEqual(len(endpoint.records),4);self.assertFalse(endpoint.failures)
            finally:channel.close();server.stop(0).wait(5);endpoint.close()


if __name__=='__main__':unittest.main()
