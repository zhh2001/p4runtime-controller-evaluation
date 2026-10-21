"""Persistent native Python adapters for the E1 boundary corpus."""

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import queue
import sys
import time

import grpc
from google.protobuf import json_format


OUTPUT = sys.stdout


def emit(value):
    print(json.dumps(value, ensure_ascii=False), file=OUTPUT, flush=True)


class NotExpressible(Exception):
    pass


def shape(c):
    return "table_entry" if c["kind"] in ("match", "param") else c["kind"] + "_entry"


class Shell:
    def __init__(self, address):
        import p4runtime_sh.shell as sh
        self.sh = sh
        sh.setup(device_id=1, grpc_addr=address, election_id=(0, 1), verbose=False)
        original = sh.client.stub
        class Deadlines:
            def Write(self, request): return original.Write(request, timeout=5)
            def Read(self, request): return original.Read(request, timeout=5)
            def __getattr__(self, name): return getattr(original, name)
        sh.client.stub = Deadlines()

    def execute(self, c):
        sh = self.sh
        kind = c["kind"]
        if kind == "register":
            raise NotExpressible("no dedicated register entity in this snapshot")
        if kind in ("match", "param"):
            entry = sh.TableEntry(c["table"])
            entry.match[c["field"]] = "0x" + c["key"]
            entry.action = sh.Action(c["action"])
            entry.action["value"] = "0x" + c["action_value"]
        else:
            entry = (sh.CounterEntry if kind == "counter" else sh.MeterEntry)(c["resource"])
            entry.index = None if c["op"] == "read" and c["index"] == -1 else c["index"]
            if c["op"] == "write":
                if kind == "counter":
                    entry.packet_count, entry.byte_count = 11, 704
                else:
                    entry.cir, entry.pir, entry.cburst, entry.pburst = 10, 20, 2, 4
        if c["op"] == "read":
            return [json_format.MessageToDict(e.msg(), preserving_proto_field_name=True) for e in entry.read()]
        entry.modify()
        return None

    def close(self):
        self.sh.teardown()


async def finsy_main(args):
    import finsy as fy
    from finsy.p4client import P4Client
    from finsy.proto import p4r, p4r_grpc
    channel = grpc.insecure_channel(args.address)
    stub = p4r_grpc.P4RuntimeStub(channel)
    requests = queue.Queue()
    def iterator():
        while (item := requests.get()) is not None:
            yield item
    stream = stub.StreamChannel(iterator())
    requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1, election_id=p4r.Uint128(low=1))))
    assert next(stream).arbitration.status.code == 0
    schema = fy.P4Schema(Path(args.p4info))
    client = P4Client(args.address)
    await client.open(schema=schema)
    emit(dict(ready=True, profile="finsy"))
    try:
        for line in sys.stdin:
            c = json.loads(line)
            if c["op"] == "close": break
            row = dict(id=c["id"], token=c["token"], stage="completed", rpc_code=0)
            started = time.monotonic_ns()
            try:
                if "finsy" in c.get("native_unexpressible", []):
                    raise NotExpressible("signed integer interface does not receive encoded sign-extension bytes")
                kind = c["kind"]
                if kind in ("match", "param"):
                    entry = fy.P4TableEntry(c["table"], match=fy.P4TableMatch({c["field"]: "0x"+c["key"]}),
                                            action=fy.P4TableAction(c["action"], value="0x"+c["action_value"]))
                else:
                    index = None if c["op"] == "read" and c["index"] == -1 else c["index"]
                    if kind == "register":
                        value = c.get("native_text") or c.get("native")
                        if value is None and c["op"] == "write": value = 0
                        entry = fy.P4RegisterEntry(c["resource"], index=index, data=value if c["op"] == "write" else None)
                    elif kind == "counter":
                        entry = fy.P4CounterEntry(c["resource"], index=index,
                                                  data=fy.P4CounterData(packet_count=11, byte_count=704) if c["op"] == "write" else None)
                    else:
                        entry = fy.P4MeterEntry(c["resource"], index=index,
                                                config=fy.P4MeterConfig(cir=10, pir=20, cburst=2, pburst=4) if c["op"] == "write" else None)
                entity = entry.encode(schema)
                if c["op"] == "write":
                    await client.request(p4r.WriteRequest(device_id=1, election_id=p4r.Uint128(low=1),
                                                          updates=[p4r.Update(type=p4r.Update.MODIFY, entity=entity)]))
                    row["public"] = None
                else:
                    row["public"] = [json_format.MessageToDict(response, preserving_proto_field_name=True)
                                      async for response in client.request_iter(p4r.ReadRequest(device_id=1, entities=[entity]))]
            except Exception as error:
                failure(row, error)
            row["elapsed_ns"] = time.monotonic_ns() - started
            emit(row)
    finally:
        await client.close()
        requests.put(None)
        stream.cancel()
        channel.close()


def failure(row, error):
    row.update(error=str(error), error_type=type(error).__name__, rpc_code=2)
    if isinstance(error, NotExpressible):
        row["stage"] = "not_expressible"
        return
    code = getattr(error, "code", None)
    if callable(code): row["rpc_code"] = code().value[0]
    elif code is not None: row["rpc_code"] = int(code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--profile", choices=("finsy", "shell"), required=True)
    parser.add_argument("--p4info", required=True)
    args = parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        if args.profile == "finsy":
            asyncio.run(finsy_main(args))
            return
        driver = Shell(args.address)
        emit(dict(ready=True, profile="shell"))
        try:
            for line in sys.stdin:
                c = json.loads(line)
                if c["op"] == "close": break
                row = dict(id=c["id"], token=c["token"], stage="completed", rpc_code=0)
                started = time.monotonic_ns()
                try: row["public"] = driver.execute(c)
                except Exception as error: failure(row, error)
                row["elapsed_ns"] = time.monotonic_ns() - started
                emit(row)
        finally:
            driver.close()


if __name__ == "__main__":
    main()
