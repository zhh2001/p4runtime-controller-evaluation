"""Verify archived E5 captures, controller records and functional outcomes."""

import argparse
import csv
from datetime import datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tarfile

from dataplane_checks import analyze


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('sdk', 'raw-current', 'legacy', 'raw-legacy', 'finsy', 'shell', 'p4ctl')
TOTALS = dict(host_frames_sent=27220, received_frames=27170, expected_drops=60, packet_ins=120,
              counter_checkpoints=360, periodic_counter_samples=2040, target_restarts=60,
              controller_processes_exited=180, cli_invocations=70)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def members(root, name, item):
    raw = gzip.decompress((root / name).read_bytes())
    require(len(raw) == item['uncompressed_bytes'] and hashlib.sha256(raw).hexdigest() == item['uncompressed_sha256'],
            f'Uncompressed archive differs: {name}')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        seen = set()
        for member in archive:
            require(member.isfile() and member.name not in seen and member.name in item['members'], 'Unexpected archive member')
            require(not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                    'Archive member escapes the output directory')
            require(member.mtime == member.uid == member.gid == 0 and member.uname == member.gname == '' and
                    member.mode == 0o644, 'Archive member metadata differs')
            seen.add(member.name)
            record = item['members'][member.name]
            data = archive.extractfile(member).read()
            require(len(data) == member.size == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                    f'Archive member SHA-256 differs: {member.name}')
            if record.get('workspace_paths_replaced'):
                require(data.count(b'<workspace>') == record['workspace_paths_replaced'] and not member.name.endswith('.pcap'),
                        'Workspace path replacement differs')
            else:
                require(record['sha256'] == record['source_sha256'], 'Original member bytes differ')
            yield member.name, data
        require(seen == set(item['members']), 'Missing archive member')


def verify_coverage(run, plan):
    require(run['stage'] == 'completed' and run['network_cleanup_completed'] and not run['race'] and
            run['profiles'] == list(PROFILES[:-1]) and run['repetitions'] == run['cli_repetitions'] == 10 and
            run['random_seed'] == 20261017 and run['cases'] == ['reactive', 'counter-recovery'] and run['plan'] == plan,
            'Incorrect formal E5 run settings')
    require(plan['expected_cases'] == 130 and plan['frame_bytes_without_fcs'] == 64 and
            plan['counter_poll_interval_seconds'] == .05, 'Incorrect E5 plan')
    schedule = []
    rng = random.Random(run['random_seed'])
    for repetition in range(1, 11):
        profiles = list(PROFILES[:-1])
        rng.shuffle(profiles)
        schedule.extend([repetition, profile, kind] for profile in profiles for kind in ('reactive', 'counter-recovery'))
    schedule.extend([repetition, 'p4ctl', 'cli'] for repetition in range(1, 11))
    labels = [f'{repetition:02}-{profile}-{kind}' for repetition, profile, kind in schedule]
    require(run['schedule'] == schedule and run['completed'] == labels and run['trial'] == len(schedule) == 130,
            'Missing cases or changed randomized schedule')
    return labels


def verify_inventory(root):
    archive = root / 'data/dataplane'
    catalog = json.loads((archive / 'manifest.json').read_text())
    require(catalog['schema_version'] == 1 and catalog['cases'] == 130 and catalog['profiles'] == list(PROFILES) and
            catalog['pcap_files'] == 520 and catalog['case_files'] == 2420 and catalog['repetitions'] == 10,
            'Unexpected E5 catalog')
    expected = {'data/dataplane/' + name for name in ('run.json', 'environment.json', 'topology.json', 'plan.json',
                'recorded-verification.json', 'case-results.csv', 'cli-invocations.csv', 'fixtures/l2.p4',
                'fixtures/l2.p4info.txt', 'fixtures/l2.compiler.txtpb', 'fixtures/schema.json')}
    expected.update('data/dataplane/records/' + profile + '.tar.gz' for profile in PROFILES)
    require(set(catalog['files']) == expected, 'Incorrect E5 file inventory')
    require({str(path.relative_to(root)) for path in archive.rglob('*') if path.is_file()} ==
            expected | {'data/dataplane/manifest.json'}, 'Unexpected E5 archive file')
    for name, item in catalog['files'].items():
        path = root / name
        require(path.resolve().is_relative_to(archive.resolve()) and path.stat().st_size == item['bytes'] and
                digest(path) == item['sha256'], f'File size or SHA-256 differs: {name}')
    require(digest(archive / 'run.json') == catalog['source_manifest_sha256'], 'Original run manifest differs')
    run = json.loads((archive / 'run.json').read_text())
    plan = json.loads((archive / 'plan.json').read_text())
    labels = verify_coverage(run, plan)
    original = json.loads((archive / 'recorded-verification.json').read_text())
    require(original['all_passed'] and original['cases'] == 130 and original['totals'] == TOTALS and
            len(original['input_files_sha256']) == 2420, 'Historical verification report differs')
    seen, pcaps = set(), 0
    for profile in PROFILES:
        name = 'data/dataplane/records/' + profile + '.tar.gz'
        for relative, data in members(root, name, catalog['files'][name]):
            require(relative not in seen and relative.split('/')[0] in labels and
                    any(relative.startswith(f'{rep:02}-{profile}-{kind}/') for rep, selected, kind in run['schedule'] if selected == profile),
                    'Duplicate or misplaced trial member')
            seen.add(relative)
            item = catalog['files'][name]['members'][relative]
            require(item['source_sha256'] == original['input_files_sha256'][relative], 'Original trial file binding differs')
            pcaps += relative.endswith('.pcap')
    require(seen == set(original['input_files_sha256']) and pcaps == 520, 'Missing original case file or PCAP')
    provenance = json.loads((root / 'config/dataplane-source-provenance.json').read_text())
    for name, item in provenance['files'].items():
        require(digest(root / name) == item['export_sha256'], f'Analysis source changed: {name}')
        original_name = item['source_file'].split('/source/', 1)[1]
        require(run['source_sha256'][original_name] == item['source_sha256'], 'Analysis differs from execution snapshot')
    for name in ('l2.p4', 'l2.p4info.txt', 'l2.compiler.txtpb'):
        require(run['source_sha256']['experiments/dataplane/fixtures/' + name] == digest(archive / 'fixtures' / name),
                'Fixture differs from execution snapshot')
    require(run['source_sha256']['experiments/dataplane/formal-plan.json'] == digest(archive / 'plan.json'), 'Original plan differs')
    schema = json.loads((archive / 'fixtures/schema.json').read_text())
    require(schema['p4info_sha256'] == digest(archive / 'fixtures/l2.p4info.txt'), 'Schema P4Info binding differs')
    return catalog


def write_csv(output, results, cli):
    with (output / 'case-results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    with (output / 'cli-invocations.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['command', 'elapsed_ns', 'exit_code', 'version_only'])
        writer.writeheader()
        writer.writerows(dict(command=' '.join(row['command']), elapsed_ns=row['elapsed_ns'], exit_code=row['exit_code'],
                              version_only=row['command'][-1] == 'version') for row in cli)


def verify(root, output):
    require(__debug__, 'Run without -O so evidence checks remain enabled')
    catalog = verify_inventory(root)
    require(not any(output.resolve().is_relative_to((root / name).resolve()) for name in ('data', 'figures')),
            'Output must be outside archived data and figures')
    output.mkdir(parents=True, exist_ok=False)
    for name, item in catalog['files'].items():
        if name.endswith('.tar.gz'):
            for relative, data in members(root, name, item):
                path = output / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    archive = root / 'data/dataplane'
    (output / 'manifest.json').write_bytes((archive / 'run.json').read_bytes())
    (output / 'schema.json').write_bytes((archive / 'fixtures/schema.json').read_bytes())
    run = json.loads((output / 'manifest.json').read_text())
    results, cli = [], []
    for trial, (repetition, profile, kind) in enumerate(run['schedule'], 1):
        folder = output / f'{repetition:02}-{profile}-{kind}'
        results.append(analyze.verify_case(folder, trial, profile, kind))
        cli.extend(row for row in analyze.rows(folder / 'events.jsonl') if row['event'] == 'cli_command')
    write_csv(output, results, cli)
    for name in ('case-results.csv', 'cli-invocations.csv'):
        require((output / name).read_bytes() == (archive / name).read_bytes(), f'Recomputed CSV differs: {name}')
    totals = {key: sum(row[key] for row in results) for key in TOTALS}
    require(totals == TOTALS, 'Recomputed E5 totals differ')
    command = [sys.executable, '-I', str(root / 'scripts/dataplane_checks/secondary.py'), str(output)]
    independent = subprocess.run(command, capture_output=True, text=True)
    (output / 'secondary.log').write_text(independent.stdout + independent.stderr)
    require(independent.returncode == 0, f'Independent verification failed. See {output / "secondary.log"}')
    secondary = json.loads((output / 'independent-verification.json').read_text())
    require(secondary['totals'] == totals and secondary['trials'] == results and secondary['pcap_files'] == 520,
            'Primary and independent reconstructions disagree')
    result = dict(verified_local=datetime.now().astimezone().isoformat(), cases=len(results), pcap_files=520, totals=totals,
                  original_case_files_checked=2420, analysis_source_correspondence_verified=True,
                  independent_reconstruction_passed=True, manifest_sha256=digest(archive / 'manifest.json'))
    (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'results/dataplane')
    args = parser.parse_args()
    result = verify(ROOT, args.output.resolve())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
