"""Inputs, process protocol, and independent data-plane assertions."""
from collections import Counter
import json
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent.parent
PROFILES = ['sdk', 'raw-current', 'legacy', 'raw-legacy', 'finsy', 'shell']
TABLE, FIELD, ACTION, COUNTER = 'MyIngress.t_l2', 'hdr.eth.dst', 'MyIngress.forward', 'MyIngress.pkt_counter'
MAGIC = b'E5PKT001'


def frame(trial, sequence, source_port, destination):
    assert 1 <= source_port <= 2 and len(bytes.fromhex(destination)) == 6
    result = bytes.fromhex(destination) + bytes.fromhex(f'02000000000{source_port}') + b'\x88\xb5'
    result += MAGIC + struct.pack('!II', trial, sequence)
    return result.ljust(64, b'\xa5')


def frame_id(value):
    assert len(value) == 64 and value[12:14] == b'\x88\xb5' and value[14:22] == MAGIC
    return struct.unpack('!II', value[22:30])


def read_pcap(path):
    data = Path(path).read_bytes()
    assert len(data) >= 24
    header = struct.unpack('<IHHIIII', data[:24])
    assert header == (0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    offset, result = 24, []
    while offset < len(data):
        assert offset + 16 <= len(data)
        _, _, included, original = struct.unpack('<IIII', data[offset:offset + 16])
        offset += 16
        assert included == original and offset + included <= len(data)
        result.append(data[offset:offset + included])
        offset += included
    return result


def check_frames(expected, observed):
    """Match port and complete payload, including duplicates and unexpected frames."""
    assert Counter(expected) == Counter(observed), {
        'missing': list((Counter(expected) - Counter(observed)).elements())[:3],
        'unexpected': list((Counter(observed) - Counter(expected)).elements())[:3]}


class Peer:
    def __init__(self, command, directory, name, popen=None):
        self.directory, self.name = Path(directory), name
        self.rows, self.condition, self.serial = [], threading.Condition(), 0
        self.ask_lock = threading.Lock()
        self.log = (self.directory / (name + '.jsonl')).open('w')
        self.errors = (self.directory / (name + '.stderr')).open('w')
        try:
            self.process = (popen or subprocess.Popen)(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                       stderr=self.errors, text=True, bufsize=1)
        except BaseException:
            self.log.close()
            self.errors.close()
            raise
        self.commands = []
        self.reader = threading.Thread(target=self._read)
        self.reader.start()
        try:
            ready = self.wait(lambda row: row.get('kind') == 'ready')
            assert 'error' not in ready, ready
        except BaseException:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.reader.join(timeout=3)
            self.process.stdout.close()
            self.process.stdin.close()
            self.log.close()
            self.errors.close()
            raise


    def _read(self):
        try:
            for line in self.process.stdout:
                row = json.loads(line)
                with self.condition:
                    self.rows.append(row)
                    self.log.write(json.dumps(row) + '\n')
                    self.log.flush()
                    self.condition.notify_all()
        except BaseException as error:
            with self.condition:
                self.rows.append({'kind': 'reader_error', 'error': repr(error)})
                self.condition.notify_all()

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for row in self.rows:
                    if predicate(row):
                        return row
                if self.process.poll() is not None:
                    raise RuntimeError(f'{self.name} exited: {self.process.returncode}; {self.rows[-2:]}')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'{self.name}: event not observed')
                self.condition.wait(min(.05, remaining))

    def ask(self, op, **values):
        with self.ask_lock:
            return self._ask(op, **values)

    def _ask(self, op, **values):
        self.serial += 1
        command = {'id': self.serial, 'op': op, **values}
        self.commands.append({'monotonic_ns': time.monotonic_ns(), **command})
        (self.directory / (self.name + '-commands.json')).write_text(json.dumps(self.commands, indent=2) + '\n')
        self.process.stdin.write(json.dumps(command) + '\n')
        self.process.stdin.flush()
        reply = self.wait(lambda row: row.get('kind') == 'reply' and row.get('id') == self.serial)
        assert reply.get('ok'), reply
        return reply

    def close(self, allow_failure=False):
        try:
            if self.process.poll() is None:
                self.ask('close')
                self.process.stdin.close()
            self.process.wait(timeout=5)
        except BaseException:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            if not allow_failure:
                raise
        finally:
            self.reader.join(timeout=3)
            assert not self.reader.is_alive(), 'Peer output reader did not stop'
            self.process.stdout.close()
            if not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            self.log.close()
            self.errors.close()
        if not allow_failure:
            assert self.process.returncode == 0, (self.name, self.process.returncode)


def driver_command(profile, address):
    info = str(ROOT / 'fixtures/l2.p4info.txt')
    if profile in ('sdk', 'raw-current', 'legacy', 'raw-legacy'):
        name = 'go-sdk' if profile in ('sdk', 'raw-current') else 'go-legacy'
        return [str(ROOT / 'bin' / name), '--address', address, '--p4info', info] + (['--raw'] if profile.startswith('raw-') else [])
    python = REPOSITORY / ('.runtime/finsy/bin/python' if profile == 'finsy' else '.runtime/shell/bin/python')
    return [str(python), str(ROOT / 'python_driver.py'), '--profile', profile, '--address', address, '--p4info', info]
