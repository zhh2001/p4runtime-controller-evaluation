import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fetch_rpc_traces import fetch_asset
from package_rpc_traces import add_member
from rpc_trace_files import archive_members, file_hash


class ArchiveTests(unittest.TestCase):
    def archive(self, directory, entries):
        path = directory / "records.tar.gz"
        records = []
        with path.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    for name, data in entries:
                        records.append(add_member(archive, name, data))
        point = {"archive": {"bytes": path.stat().st_size, "sha256": file_hash(path)}}
        return path, point, {"files": records}

    def test_deterministic_archive_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            entries = [("default/phase-0.json", b'{"attempts": 2}\n'),
                       ("default/state-0.pb.gz", gzip.compress(b"state", mtime=0))]
            path, point, inventory = self.archive(directory, entries)
            first = path.read_bytes()
            self.assertEqual([data for _, data in archive_members(path, point, inventory)],
                             [data for _, data in entries])
            self.archive(directory, entries)
            self.assertEqual(first, path.read_bytes())

    def test_changed_member_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, point, inventory = self.archive(Path(temporary), [("default/phase-0.json", b"{}")])
            inventory["files"][0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "member hash differs"):
                list(archive_members(path, point, inventory))

    def test_reordered_or_extra_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, point, inventory = self.archive(Path(temporary),
                [("default/phase-0.json", b"{}"), ("default/state-0.pb.gz", b"data")])
            with self.assertRaisesRegex(ValueError, "order differs"):
                list(archive_members(path, point, {"files": inventory["files"][::-1]}))
            with self.assertRaisesRegex(ValueError, "extra archive member"):
                list(archive_members(path, point, {"files": inventory["files"][:1]}))

    def test_changed_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, point, inventory = self.archive(Path(temporary), [("default/phase-0.json", b"{}")])
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "File size differs"):
                list(archive_members(path, point, inventory))

    def test_local_path_is_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Local text found"):
                self.archive(Path(temporary), [("default/run.json", b'{"path":"/home/example/file"}')])


class DownloadTests(unittest.TestCase):
    def test_download_and_cached_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.tar.gz"
            data = b"archive data"
            record = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            with patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as request:
                self.assertTrue(fetch_asset("https://example.org/archive", path, record))
                self.assertFalse(fetch_asset("https://example.org/archive", path, record))
                self.assertEqual(request.call_count, 1)
            self.assertEqual(path.read_bytes(), data)

    def test_failed_download_leaves_no_archive(self):
        for received in (b"short", b"longer than expected", b"wrong bytes"):
            with self.subTest(received=received), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "archive.tar.gz"
                data = b"valid bytes"
                record = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                with patch("urllib.request.urlopen", return_value=io.BytesIO(received)):
                    with self.assertRaises(ValueError):
                        fetch_asset("https://example.org/archive", path, record)
                self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
