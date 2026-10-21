"""Raw Ethernet sender and capture agent, run inside one Mininet host."""
import argparse
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--interface', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    lock = threading.Lock()
    stopping = threading.Event()
    captures = {}
    for direction in ('tx', 'rx'):
        file = (args.directory / (direction + '.pcap')).open('wb')
        file.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
        captures[direction] = file

    def emit(row):
        with lock:
            print(json.dumps(row), flush=True)

    def capture(direction, frame):
        epoch = time.time_ns()
        file = captures[direction]
        file.write(struct.pack('<IIII', epoch // 10**9, epoch % 10**9 // 1000, len(frame), len(frame)))
        file.write(frame)
        file.flush()

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 2**20)
    sock.bind((args.interface, 0))
    sock.settimeout(.1)

    def receive():
        while not stopping.is_set():
            try:
                frame, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if address[2] == socket.PACKET_OUTGOING or frame[12:14] != b'\x88\xb5':
                continue
            capture('rx', frame)
            emit({'kind': 'rx', 'payload': frame.hex(), 'monotonic_ns': time.monotonic_ns()})

    receiver = threading.Thread(target=receive)
    receiver.start()
    emit({'kind': 'ready', 'interface': args.interface})
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command['op'] == 'close':
                emit({'kind': 'reply', 'id': command['id'], 'ok': True})
                break
            if command['op'] != 'send':
                raise ValueError(command['op'])
            for payload in command['frames']:
                frame = bytes.fromhex(payload)
                before = time.monotonic_ns()
                sent = sock.send(frame)
                assert sent == len(frame)
                capture('tx', frame)
                emit({'kind': 'tx', 'payload': payload, 'monotonic_ns': before})
                if command.get('interval_seconds', 0):
                    time.sleep(command['interval_seconds'])
            emit({'kind': 'reply', 'id': command['id'], 'ok': True})
    finally:
        stopping.set()
        receiver.join(timeout=2)
        assert not receiver.is_alive()
        sock.close()
        for file in captures.values():
            file.close()


if __name__ == '__main__':
    main()
