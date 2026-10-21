"""Verify newly executed Mininet data-plane cases with both decoders."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import random
import subprocess
import sys

from bootstrap_dataplane import ROOT, check_setup, selection, digest
from dataplane_checks import analyze
from verify_dataplane import require, write_csv, TOTALS


def verify_coverage(output, setup):
    run = json.loads((output / 'manifest.json').read_text())
    require(run['stage'] == 'sampling_completed' and run['network_cleanup_completed'], 'Incomplete data-plane run')
    configuration = selection(run['run_mode'], run['profiles'], run['skip_cli'])
    require(all(run[key] == value for key, value in configuration.items()), 'Data-plane mode or repetitions differ')
    plan = json.loads((ROOT / 'data/dataplane/plan.json').read_text())
    require(run['plan'] == plan and run['random_seed'] == plan['random_seed'], 'Data-plane plan differs')
    schedule, rng = [], random.Random(plan['random_seed'])
    for repetition in range(1, run['repetitions'] + 1):
        profiles = list(run['profiles'])
        rng.shuffle(profiles)
        schedule.extend([repetition, profile, kind] for profile in profiles for kind in ('reactive', 'counter-recovery'))
    schedule.extend([repetition, 'p4ctl', 'cli'] for repetition in range(1, run['cli_repetitions'] + 1))
    labels = [f'{repetition:02}-{profile}-{kind}' for repetition, profile, kind in schedule]
    require(run['schedule'] == schedule and run['completed'] == labels and run['trial'] == len(labels),
            'Missing cases or changed randomized schedule')
    expected = {**setup['input_sha256'], **setup['generated_sha256']}
    require(run['source_sha256'] == expected, 'Data-plane snapshot inventory differs from setup')
    for name, value in expected.items():
        require(digest(output / 'source' / name) == value, f'Data-plane source snapshot changed: {name}')
    require((output / 'schema.json').read_bytes() == (ROOT / 'data/dataplane/fixtures/schema.json').read_bytes(), 'Data-plane schema differs')
    artifacts = json.loads((output / 'artifact-hashes.json').read_text())
    required = {'manifest.json', 'environment.json', 'schema.json', 'topology.json', 'network-cleanup.json'}
    for label in labels:
        required.update(str(path.relative_to(output)) for path in (output / label).rglob('*') if path.is_file())
    required.update('source/' + name for name in expected)
    require(set(artifacts) == required, 'Data-plane artifact inventory differs')
    for name, value in artifacts.items():
        require((output / name).resolve().is_relative_to(output.resolve()) and digest(output / name) == value,
                f'Data-plane artifact changed: {name}')
    topology = json.loads((output / 'topology.json').read_text())
    cleanup = json.loads((output / 'network-cleanup.json').read_text())
    require(cleanup['interfaces_removed'] == {name: True for name in topology['bmv2_interfaces']} and
            cleanup['namespace_processes_stopped'] == {str(item['namespace_pid']): True for item in topology['hosts'].values()},
            'Data-plane network cleanup differs')
    for label in labels:
        targets = json.loads((output / label / 'target-processes.json').read_text())
        for target in targets:
            command = target['command']
            ipc = command[command.index('--notifications-addr') + 1]
            require(not Path(ipc[6:]).exists(), 'Owned BMv2 IPC file remains')
            bindings = [command[index + 1] for index, value in enumerate(command) if value == '-i']
            require(bindings == [f'{port}@{name}' for port, name in enumerate(topology['bmv2_interfaces'], 1)], 'Target topology differs')
    return run


def verify(output):
    setup = check_setup()
    run = verify_coverage(output, setup)
    results, cli = [], []
    for trial, (repetition, profile, kind) in enumerate(run['schedule'], 1):
        folder = output / f'{repetition:02}-{profile}-{kind}'
        results.append(analyze.verify_case(folder, trial, profile, kind))
        cli.extend(row for row in analyze.rows(folder / 'events.jsonl') if row['event'] == 'cli_command')
    write_csv(output, results, cli)
    command = [sys.executable, '-I', str(ROOT / 'scripts/dataplane_checks/secondary.py'), str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    (output / 'secondary.log').write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f'Independent data-plane verification failed. See {output / "secondary.log"}')
    independent = json.loads((output / 'independent-verification.json').read_text())
    totals = {key: sum(row[key] for row in results) for key in TOTALS}
    require(independent['trials'] == results and independent['totals'] == totals, 'Data-plane decoders disagree')
    check_setup()
    record = dict(verified_local=datetime.now().astimezone().isoformat(), cases=len(results), mode=run['run_mode'],
                  pcap_files=len(results) * 4, totals=totals, source_snapshot_verified=True,
                  independent_reconstruction_passed=True, network_cleanup_verified=True)
    (output / 'verification.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    verify(args.run.resolve())
