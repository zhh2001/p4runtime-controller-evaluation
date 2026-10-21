"""Persistent native Python Write clients. The common Read observes desired state."""

import argparse
import asyncio
import base64
import contextlib
import json
import queue
import sys

import grpc

OUTPUT = sys.stdout


def emit(row): print(json.dumps(row), file=OUTPUT, flush=True)


def rpc_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, grpc.RpcError): return error
        error = error.__context__
    return None


def raw_fields(error):
    if error is None: return {}
    result = dict(rpc_code=error.code().value[0])
    for key, value in error.trailing_metadata() or ():
        if key == 'grpc-status-details-bin': result['raw_status'] = base64.b64encode(value).decode()
    return result


def arbitration(stub, p4r):
    requests = queue.Queue()
    def iterator():
        while (value := requests.get()) is not None: yield value
    call = stub.StreamChannel(iterator())
    requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1))))
    assert next(call).arbitration.status.code == 0
    return requests, call


async def finsy_main(args):
    from finsy.p4client import P4Client, P4ClientError
    from finsy.proto import p4r, p4r_grpc
    from google.protobuf import json_format
    channel = grpc.insecure_channel(args.address)
    stub = p4r_grpc.P4RuntimeStub(channel)
    requests, call = arbitration(stub,p4r)
    client = P4Client(args.address)
    await client.open()
    emit(dict(ready=True,profile='finsy'))
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command['op'] == 'close': break
            row = dict(token=command['token'],rpc_code=0,native_results=None)
            if command['op'] == 'read':
                replies = stub.Read(p4r.ReadRequest(device_id=1,entities=[p4r.Entity(table_entry=p4r.TableEntry())]),timeout=5)
                row['entities'] = [json_format.MessageToDict(e,preserving_proto_field_name=True) for reply in replies for e in reply.entities]
            else:
                request = json_format.ParseDict(dict(device_id='1',election_id=dict(low='1'),updates=command['updates']),p4r.WriteRequest())
                try: await client.request(request)
                except Exception as error:
                    row.update(error=str(error),error_type=type(error).__name__,rpc_code=2)
                    row.update(raw_fields(rpc_error(error)))
                    if isinstance(error,P4ClientError):
                        row['native_results'] = [dict(index=i,code=int(v.canonical_code)) for i,v in error.details.items()]
            emit(row)
    finally:
        await client.close();requests.put(None);call.cancel();channel.close()


def shell_main(args):
    from google.protobuf import json_format
    from p4.v1 import p4runtime_pb2 as p4r
    import p4runtime_sh.shell as sh
    from p4runtime_sh.p4runtime import P4RuntimeWriteException
    sh.setup(device_id=1,grpc_addr=args.address,election_id=(0,1),verbose=False)
    original = sh.client.stub
    class Deadlines:
        last_error = None
        def Write(self,request):
            self.last_error = None
            try: return original.Write(request,timeout=5)
            except grpc.RpcError as error:
                self.last_error = error
                raise
        def __getattr__(self,name): return getattr(original,name)
    deadline = Deadlines()
    sh.client.stub = deadline
    emit(dict(ready=True,profile='shell'))
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command['op'] == 'close': break
            row = dict(token=command['token'],rpc_code=0,native_results=None)
            if command['op'] == 'read':
                replies = original.Read(p4r.ReadRequest(device_id=1,entities=[p4r.Entity(table_entry=p4r.TableEntry())]),timeout=5)
                row['entities'] = [json_format.MessageToDict(e,preserving_proto_field_name=True) for reply in replies for e in reply.entities]
            else:
                request = json_format.ParseDict(dict(updates=command['updates']),p4r.WriteRequest())
                try: sh.client.write(request)
                except Exception as error:
                    row.update(error=str(error),error_type=type(error).__name__,rpc_code=2)
                    row.update(raw_fields(deadline.last_error))
                    if isinstance(error,P4RuntimeWriteException):
                        row['native_results'] = [dict(index=i,code=v.canonical_code) for i,v in error.errors]
            emit(row)
    finally: sh.teardown()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',required=True);parser.add_argument('--address',required=True)
    args=parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        if args.profile == 'finsy': asyncio.run(finsy_main(args))
        else: shell_main(args)


if __name__ == '__main__': main()
