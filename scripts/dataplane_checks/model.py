"""Original E5 frame construction, identity and PCAP checks."""

from collections import Counter
from pathlib import Path
import struct

PROFILES = ['sdk', 'raw-current', 'legacy', 'raw-legacy', 'finsy', 'shell']
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
