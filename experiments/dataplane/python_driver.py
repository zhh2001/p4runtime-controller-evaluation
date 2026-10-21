"""Native Finsy and p4runtime-shell operations with a common JSON protocol."""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import queue
import sys
import threading
import time

from common import ACTION, COUNTER, FIELD, TABLE

OUTPUT = sys.stdout
LOCK = threading.Lock()


def emit(row):
    row['monotonic_ns'] = time.monotonic_ns()
    with LOCK:
        print(json.dumps(row), file=OUTPUT, flush=True)


def incoming(payload, port):
    if payload[12:14] == b'\x88\xb5':
        emit({'kind': 'packet', 'payload': payload.hex(), 'ingress_port': port})


class Shell:
    def __init__(self, address):
        import p4runtime_sh.shell as sh
        from p4.v1 import p4runtime_pb2 as p4r
        sh.setup(device_id=1, grpc_addr=address, election_id=(0, 1), verbose=False)
        self.sh, self.p4r = sh, p4r
        self.stopping = threading.Event()
        self.packets = sh.PacketIn()

        def receive():
            while not self.stopping.is_set():
                for value in self.packets.sniff(timeout=.01):
                    value = value.packet
                    port = next(int.from_bytes(m.value, 'big') for m in value.metadata if m.metadata_id == 1)
                    incoming(value.payload, port)

        self.receiver = threading.Thread(target=receive)
        self.receiver.start()

    def operation(self, command):
        if command['op'] == 'write':
            request = self.p4r.WriteRequest()
            kind = self.p4r.Update.MODIFY if command['mode'] == 'modify' else self.p4r.Update.INSERT
            for value in command['entries']:
                item = self.sh.TableEntry(TABLE)
                item.match[FIELD] = str(int(value['mac'], 16))
                item.action = self.sh.Action(ACTION)
                item.action['port'] = str(value['port'])
                update = request.updates.add(type=kind)
                update.entity.table_entry.CopyFrom(item.msg())
            self.sh.client.write(request)
            return {}
        if command['op'] == 'send':
            self.sh.PacketOut(bytes.fromhex(command['payload']), egress_port=str(command['port']), _pad='0').send()
            return {}
        if command['op'] == 'counter':
            selector = self.sh.CounterEntry(COUNTER)
            selector.index = command['index']
            items = list(selector.read())
            assert len(items) == 1
            data = items[0].msg().data
            return {'packets': data.packet_count, 'bytes': data.byte_count}
        raise ValueError(command['op'])

    def close(self):
        self.stopping.set()
        self.receiver.join(timeout=2)
        assert not self.receiver.is_alive()
        self.sh.teardown()


async def finsy_main(args):
    import finsy as fy
    ready = asyncio.Event()
    async def on_ready(switch):
        ready.set()
    switch = fy.Switch('e5', args.address, options=fy.SwitchOptions(
        p4info=args.p4info, device_id=1, initial_election_id=1, ready_handler=on_ready))
    session = asyncio.create_task(switch.run())
    async def packets():
        async for packet in switch.read_packets(eth_types=[0x88b5]):
            incoming(packet.payload, packet['ingress_port'])
    receiver = asyncio.create_task(packets())
    try:
        await asyncio.wait_for(ready.wait(), 15)
        assert switch.is_primary and switch.is_up
        emit({'kind': 'ready'})
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            row = {'kind': 'reply', 'id': command['id'], 'op': command['op'], 'ok': True}
            try:
                if command['op'] == 'close':
                    emit(row)
                    break
                if command['op'] == 'write':
                    entries = [fy.P4TableEntry(TABLE, match=fy.P4TableMatch({FIELD: int(v['mac'], 16)}),
                                              action=fy.P4TableAction(ACTION, port=v['port'])) for v in command['entries']]
                    await switch.write([~v if command['mode'] == 'modify' else +v for v in entries])
                elif command['op'] == 'counter':
                    found = [item async for item in switch.read([fy.P4CounterEntry(COUNTER, index=command['index'])])]
                    assert len(found) == 1
                    data = found[0].data or fy.P4CounterData()
                    row.update(packets=data.packet_count, bytes=data.byte_count)
                elif command['op'] == 'send':
                    await switch.write([fy.P4PacketOut(bytes.fromhex(command['payload']), egress_port=command['port'], _pad=0)])
                else:
                    raise ValueError(command['op'])
            except BaseException as error:
                row.update(ok=False, error=repr(error))
            emit(row)
    finally:
        receiver.cancel()
        session.cancel()
        await asyncio.gather(receiver, session, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=['finsy', 'shell'], required=True)
    parser.add_argument('--address', required=True)
    parser.add_argument('--p4info', type=Path, required=True)
    args = parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        if args.profile == 'finsy':
            asyncio.run(finsy_main(args))
        else:
            backend = Shell(args.address)
            try:
                emit({'kind': 'ready'})
                for line in sys.stdin:
                    command = json.loads(line)
                    row = {'kind': 'reply', 'id': command['id'], 'op': command['op'], 'ok': True}
                    if command['op'] == 'close':
                        emit(row)
                        break
                    try:
                        row.update(backend.operation(command))
                    except BaseException as error:
                        row.update(ok=False, error=repr(error))
                    emit(row)
            finally:
                backend.close()


if __name__ == '__main__':
    main()
