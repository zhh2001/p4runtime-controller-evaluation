"""Independent protobuf observer and common pipeline installation."""
import json
import queue

import grpc
from google.protobuf import json_format, text_format
from finsy.proto import p4i, p4r, p4r_grpc

from common import COUNTER, ROOT, TABLE

INFO = text_format.Parse((ROOT / 'fixtures/l2.p4info.txt').read_text(), p4i.P4Info())
TABLE_ID = next(t.preamble.id for t in INFO.tables if t.preamble.name == TABLE)
COUNTER_ID = next(c.preamble.id for c in INFO.counters if c.preamble.name == COUNTER)


class Observer:
    def __init__(self, address, trace):
        self.channel = grpc.insecure_channel(address)
        self.stub = p4r_grpc.P4RuntimeStub(self.channel)
        self.trace = trace

    def install(self):
        requests = queue.Queue()
        def iterate():
            while (item := requests.get()) is not None:
                yield item
        stream = self.stub.StreamChannel(iterate())
        try:
            requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(
                device_id=1, election_id=p4r.Uint128(low=1))))
            response = next(stream)
            assert response.HasField('arbitration') and response.arbitration.status.code == 0
            self.stub.SetForwardingPipelineConfig(p4r.SetForwardingPipelineConfigRequest(
                device_id=1, election_id=p4r.Uint128(low=1), action=p4r.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT,
                config=p4r.ForwardingPipelineConfig(p4info=INFO, p4_device_config=(ROOT / 'fixtures/l2.json').read_bytes())), timeout=10)
            self.trace('pipeline_installed')
        finally:
            requests.put(None)
            stream.cancel()

    def missing_pipeline(self):
        try:
            response = self.stub.GetForwardingPipelineConfig(p4r.GetForwardingPipelineConfigRequest(
                device_id=1, response_type=p4r.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE), timeout=5)
        except grpc.RpcError as error:
            assert error.code() == grpc.StatusCode.FAILED_PRECONDITION
            self.trace('pipeline_missing', rpc_code=error.code().value[0])
            return
        assert not response.config.p4info.tables, 'target retained a pipeline after a fresh start'
        self.trace('pipeline_missing', rpc_code=0, protobuf=json_format.MessageToDict(response))

    def tables(self):
        request = p4r.ReadRequest(device_id=1, entities=[p4r.Entity(table_entry=p4r.TableEntry(table_id=TABLE_ID))])
        entries, raw = [], []
        for response in self.stub.Read(request, timeout=5):
            raw.append(json_format.MessageToDict(response, preserving_proto_field_name=True))
            for entity in response.entities:
                item = entity.table_entry
                if item.is_default_action:
                    continue
                assert len(item.match) == len(item.action.action.params) == 1
                entries.append({'mac': f'{int.from_bytes(item.match[0].exact.value, "big"):012x}',
                                'port': int.from_bytes(item.action.action.params[0].value, 'big')})
        self.trace('table_read', entries=entries, protobuf=raw)
        return sorted(entries, key=lambda e: e['mac'])

    def counters(self):
        result = {}
        for index in (1, 2):
            request = p4r.ReadRequest(device_id=1, entities=[p4r.Entity(counter_entry=p4r.CounterEntry(
                counter_id=COUNTER_ID, index=p4r.Index(index=index)))])
            found = [e.counter_entry for r in self.stub.Read(request, timeout=5) for e in r.entities]
            assert len(found) == 1
            result[index] = {'packets': found[0].data.packet_count, 'bytes': found[0].data.byte_count}
        self.trace('counter_read', values=result)
        return result

    def close(self):
        self.channel.close()
