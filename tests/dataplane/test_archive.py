import ast
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_dataplane as verify
from dataplane_checks import analyze, secondary


def functions(source):
    result = {}
    def walk(node, prefix=''):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    result[name] = ast.get_source_segment(source, child)
                walk(child, name + '.')
            else:
                walk(child, prefix)
    walk(ast.parse(source))
    return result


class InventoryTests(unittest.TestCase):
    def test_complete_file_and_original_pcap_inventory(self):
        catalog = verify.verify_inventory(ROOT)
        self.assertEqual(len(catalog['files']), 18)
        members = [member for item in catalog['files'].values() for name, member in item.get('members', {}).items() if name.endswith('.pcap')]
        self.assertEqual(len(members), 520)
        self.assertTrue(all(item['sha256'] == item['source_sha256'] for item in members))

    def test_original_function_and_test_correspondence(self):
        provenance = json.loads((ROOT / 'config/dataplane-source-provenance.json').read_text())
        for name, item in provenance['files'].items():
            actual = functions((ROOT / name).read_text())
            for function, expected in item['unchanged_functions_sha256'].items():
                self.assertEqual(hashlib.sha256(actual[function].encode()).hexdigest(), expected, (name, function))
        tests = provenance['files']['tests/dataplane/test_frames.py']['unchanged_functions_sha256']
        self.assertEqual(sum('.test_' in name for name in tests), 4)

    def mutate_catalog(self, mutation):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / 'data/dataplane', root / 'data/dataplane')
            path = root / 'data/dataplane/manifest.json'
            catalog = json.loads(path.read_text())
            mutation(root, catalog)
            path.write_text(json.dumps(catalog))
            verify.verify_inventory(root)

    def test_missing_archive_inventory_entry_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'file inventory'):
            self.mutate_catalog(lambda root, catalog: catalog['files'].pop('data/dataplane/records/sdk.tar.gz'))

    def test_unlisted_data_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected E5 archive file'):
            self.mutate_catalog(lambda root, catalog: (root / 'data/dataplane/extra.txt').write_text('extra'))

    def test_changed_compressed_archive_is_rejected(self):
        def mutation(root, catalog):
            path = root / 'data/dataplane/records/sdk.tar.gz'
            path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.mutate_catalog(mutation)

    def test_changed_uncompressed_digest_is_rejected(self):
        def mutation(root, catalog):
            catalog['files']['data/dataplane/records/sdk.tar.gz']['uncompressed_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Uncompressed archive'):
            self.mutate_catalog(mutation)

    def test_changed_original_member_binding_is_rejected(self):
        def mutation(root, catalog):
            item = catalog['files']['data/dataplane/records/sdk.tar.gz']['members']
            member = next(value for value in item.values() if value.get('workspace_paths_replaced'))
            member['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Original trial file binding'):
            self.mutate_catalog(mutation)

    def test_tar_rejects_duplicate_and_escaping_members(self):
        for names in [('trial/file', 'trial/file'), ('../escape',)]:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                data = b'value'
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w') as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.size, member.mode = len(data), 0o644
                        archive.addfile(member, io.BytesIO(data))
                raw = buffer.getvalue()
                (root / 'sample.tar.gz').write_bytes(gzip.compress(raw, mtime=0))
                item = dict(uncompressed_bytes=len(raw), uncompressed_sha256=hashlib.sha256(raw).hexdigest(),
                            members={name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                                source_sha256=hashlib.sha256(data).hexdigest()) for name in names})
                with self.assertRaises(ValueError):
                    list(verify.members(root, 'sample.tar.gz', item))


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata = json.loads((ROOT / 'data/dataplane/run.json').read_text())
        cls.plan = json.loads((ROOT / 'data/dataplane/plan.json').read_text())

    def test_complete_schedule(self):
        self.assertEqual(len(verify.verify_coverage(self.metadata, self.plan)), 130)

    def test_missing_case_is_rejected(self):
        run = copy.deepcopy(self.metadata)
        run['completed'].pop()
        with self.assertRaisesRegex(ValueError, 'Missing cases'):
            verify.verify_coverage(run, self.plan)

    def test_changed_randomized_schedule_is_rejected(self):
        run = copy.deepcopy(self.metadata)
        run['schedule'][0], run['schedule'][1] = run['schedule'][1], run['schedule'][0]
        with self.assertRaisesRegex(ValueError, 'randomized schedule'):
            verify.verify_coverage(run, self.plan)

    def test_missing_network_cleanup_is_rejected(self):
        run = dict(self.metadata, network_cleanup_completed=False)
        with self.assertRaisesRegex(ValueError, 'formal E5 run settings'):
            verify.verify_coverage(run, self.plan)


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / 'data/dataplane/fixtures/schema.json').read_text())
        cls.metadata = json.loads((ROOT / 'data/dataplane/run.json').read_text())
        cls.archive = json.loads((ROOT / 'data/dataplane/manifest.json').read_text())

    def sample(self, kind, mutation=None, extra_check=False):
        profile = 'p4ctl' if kind == 'cli' else 'sdk'
        trial, job = next((index, job) for index, job in enumerate(self.metadata['schedule'], 1) if job[1:] == [profile, kind])
        label = f'{job[0]:02}-{profile}-{kind}'
        name = 'data/dataplane/records/' + profile + '.tar.gz'
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / label
            for relative, data in verify.members(ROOT, name, self.archive['files'][name]):
                if relative.startswith(label + '/'):
                    path = Path(temporary) / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            if mutation:
                mutation(folder)
            if extra_check:
                analyze.verify_case(folder, trial, profile, kind)
            return secondary.verify_case(folder, trial, profile, kind, self.schema)

    def update_jsonl(self, folder, name, mutation):
        path = folder / name
        rows = secondary.records(path)
        mutation(rows)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_independent_decoder_accepts_all_three_scenarios(self):
        for kind in ('reactive', 'counter-recovery', 'cli'):
            with self.subTest(kind=kind):
                self.assertGreater(self.sample(kind, extra_check=True)['received_frames'], 0)

    def test_missing_or_duplicate_capture_is_rejected(self):
        for duplicate in (False, True):
            def mutation(folder):
                path = folder / 'h1/rx.pcap'
                data = path.read_bytes()
                path.write_bytes(data + data[24:104] if duplicate else data[:24] + data[104:])
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(AssertionError, 'PCAP port or content'):
                self.sample('reactive', mutation)

    def test_host_log_must_match_pcap(self):
        def mutation(folder):
            self.update_jsonl(folder, 'host1.jsonl', lambda rows: rows.pop(next(i for i, row in enumerate(rows) if row['kind'] == 'rx')))
        with self.assertRaisesRegex(AssertionError, 'Host log versus PCAP'):
            self.sample('reactive', mutation, extra_check=True)

    def test_raw_table_read_is_decoded(self):
        def mutation(folder):
            def change(rows):
                table = next(row for row in rows if row['event'] == 'table_read')
                param = table['protobuf'][0]['entities'][0]['table_entry']['action']['action']['params'][0]
                param['value'] = 'AQ=='
            self.update_jsonl(folder, 'events.jsonl', change)
        with self.assertRaisesRegex(AssertionError, 'Raw table read'):
            self.sample('reactive', mutation, extra_check=True)

    def test_raw_counter_reply_is_cross_checked(self):
        def mutation(folder):
            def change(rows):
                row = next(row for row in rows if row.get('op') == 'counter')
                row['packets'] += 1
                row['bytes'] += 64
            self.update_jsonl(folder, 'driver-1.jsonl', change)
        with self.assertRaisesRegex(AssertionError, 'Counter samples versus raw driver replies'):
            self.sample('counter-recovery', mutation, extra_check=True)

    def test_driver_reply_cannot_be_omitted(self):
        def mutation(folder):
            self.update_jsonl(folder, 'driver-1.jsonl', lambda rows: rows.pop(next(i for i, row in enumerate(rows) if row['kind'] == 'reply')))
        with self.assertRaisesRegex(AssertionError, 'Reply IDs'):
            self.sample('reactive', mutation, extra_check=True)

    def test_batch_modify_intent_is_preserved(self):
        def mutation(folder):
            path = folder / 'driver-1-commands.json'
            rows = json.loads(path.read_text())
            next(row for row in rows if row.get('mode') == 'modify')['mode'] = 'insert'
            path.write_text(json.dumps(rows))
        with self.assertRaisesRegex(AssertionError, 'Batch write intent'):
            self.sample('counter-recovery', mutation, extra_check=True)

    def test_cli_explicit_packetout_port_is_checked(self):
        def mutation(folder):
            def change(rows):
                command = [row for row in rows if row['event'] == 'cli_command'][-1]['command']
                command[command.index('--port') + 1] = '1'
            self.update_jsonl(folder, 'events.jsonl', change)
        with self.assertRaises(AssertionError):
            self.sample('cli', mutation, extra_check=True)

    def test_cli_elapsed_interval_is_recomputed(self):
        def mutation(folder):
            def change(rows):
                next(row for row in rows if row['event'] == 'cli_command')['elapsed_ns'] += 1
            self.update_jsonl(folder, 'events.jsonl', change)
        with self.assertRaises(AssertionError):
            self.sample('cli', mutation, extra_check=True)


if __name__ == '__main__':
    unittest.main()
