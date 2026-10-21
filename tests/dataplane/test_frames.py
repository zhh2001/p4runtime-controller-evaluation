import struct
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from dataplane_checks.model import check_frames, frame, frame_id, read_pcap
from dataplane_checks.analyze import expected_frames


class FrameTests(unittest.TestCase):
    def test_payload_identity_and_ethernet_fields(self):
        value = frame(17, 4096, 1, '020000000002')
        self.assertEqual(len(value), 64)
        self.assertEqual(value[:14], bytes.fromhex('02000000000202000000000188b5'))
        self.assertEqual(frame_id(value), (17, 4096))

    def test_capture_check_detects_loss_duplicates_and_wrong_ports(self):
        expected = [(2, 'abcd'), (1, '1234')]
        check_frames(expected, expected[::-1])
        for bad in [expected[:1], expected + expected[:1], [(1, 'abcd'), (1, '1234')]]:
            with self.assertRaises(AssertionError):
                check_frames(expected, bad)

    def test_rejects_truncated_pcap(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'capture.pcap'
            path.write_bytes(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
                             + struct.pack('<IIII', 1, 2, 64, 64) + b'short')
            with self.assertRaises(AssertionError):
                read_pcap(path)

    def test_case_packet_totals_and_restart_probe(self):
        for kind, tx_count, rx_count in [('reactive', 66, 66), ('counter-recovery', 385, 384), ('cli', 16, 17)]:
            tx, rx = expected_frames(3, kind)
            self.assertEqual((len(tx), len(rx)), (tx_count, rx_count))
            self.assertEqual(len({p for _, p in rx}), rx_count)
            if kind == 'counter-recovery':
                self.assertEqual(len(set(p for _, p in tx) - set(p for _, p in rx)), 1)


if __name__ == '__main__':
    unittest.main()
