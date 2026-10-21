from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyze import public_count


class PublicResultsTests(unittest.TestCase):
    def test_zero_single_sample_is_present(self):
        self.assertEqual(public_count(dict(profile="legacy",public={})),1)

    def test_fields_are_not_multiple_samples(self):
        self.assertEqual(public_count(dict(profile="legacy",public=dict(packet_count=11,byte_count=704))),1)

    def test_finsy_read_responses_contain_entities(self):
        self.assertEqual(public_count(dict(profile="finsy",public=[dict(entities=[{},{}]),{}])),2)

    def test_sdk_returns_samples_directly(self):
        self.assertEqual(public_count(dict(profile="sdk",public=[{},{}])),2)
