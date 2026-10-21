import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_release


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.tag = 'evaluation-v1'
        self.prefix = 'evaluation'
        self.content = {'README.md': b'Example\n', 'data/record.json': b'{"value": 1}\n'}
        self.archive = self.directory / (self.tag + '.tar.gz')
        self.manifest_path = self.directory / (self.tag + '-manifest.json')
        files = {}
        for name, data in self.content.items():
            blob = b'blob ' + str(len(data)).encode() + b'\0' + data
            files[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                               git_blob=hashlib.sha1(blob).hexdigest(), mode='644')
        self.manifest = dict(schema_version=1, release=dict(schema_version=1, tag=self.tag,
                             source_commit='a' * 40, archive_prefix=self.prefix), files=files)
        self.write_archive(list(self.content.items()))
        self.refresh_container()

    def write_archive(self, files):
        with self.archive.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|') as stream:
                for name, data in files:
                    member = tarfile.TarInfo(self.prefix + '/' + name)
                    member.mode, member.size = 0o644, len(data)
                    stream.addfile(member, io.BytesIO(data))

    def refresh_container(self):
        self.manifest['archive'] = dict(bytes=self.archive.stat().st_size,
                                        sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.manifest_path.write_text(json.dumps(self.manifest) + '\n')
        (self.directory / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
            for path in (self.archive, self.manifest_path)))

    def test_valid_release_without_git_metadata(self):
        result = package_release.verify_package(self.directory)
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['source_commit'], 'a' * 40)
        self.assertFalse((self.directory / '.git').exists())

    def test_changed_download_is_rejected(self):
        for path in (self.archive, self.manifest_path):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                with self.assertRaisesRegex(ValueError, 'Release checksum differs'):
                    package_release.verify_package(self.directory)
                path.write_bytes(original)

    def test_changed_file_with_new_container_checksum_is_rejected(self):
        self.write_archive([('README.md', b'Different\n'), ('data/record.json', self.content['data/record.json'])])
        self.refresh_container()
        with self.assertRaisesRegex(ValueError, 'file content or mode differs'):
            package_release.verify_package(self.directory)

    def test_missing_file_is_rejected(self):
        self.write_archive([('README.md', self.content['README.md'])])
        self.refresh_container()
        with self.assertRaisesRegex(ValueError, 'Missing or extra archive files'):
            package_release.verify_package(self.directory)

    def test_duplicate_file_is_rejected(self):
        self.write_archive([*self.content.items(), ('README.md', b'Example\n')])
        self.refresh_container()
        with self.assertRaisesRegex(ValueError, 'Duplicate archive member'):
            package_release.verify_package(self.directory)

    def test_path_traversal_is_rejected(self):
        self.write_archive([('../outside', b'Example\n')])
        self.refresh_container()
        with self.assertRaisesRegex(ValueError, 'Invalid archive path'):
            package_release.verify_package(self.directory)

    def test_checksum_path_or_duplicate_is_rejected(self):
        original = (self.directory / 'SHA256SUMS').read_text()
        for extra in ('0' * 64 + '  ../outside\n', original.splitlines()[0] + '\n'):
            with self.subTest(extra=extra):
                (self.directory / 'SHA256SUMS').write_text(original + extra)
                with self.assertRaisesRegex(ValueError, 'Unexpected checksum name'):
                    package_release.verify_package(self.directory)

    def test_unknown_manifest_version_is_rejected(self):
        self.manifest['schema_version'] = 2
        self.refresh_container()
        with self.assertRaisesRegex(ValueError, 'Unsupported release manifest version'):
            package_release.verify_package(self.directory)


if __name__ == '__main__':
    unittest.main()
