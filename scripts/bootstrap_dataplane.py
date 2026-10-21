"""Install pinned clients and build the Mininet data-plane experiments."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from bootstrap_rpc import ROOT, RUNTIME, command, digest, environment, install_source, inventory
from bootstrap_construction import install_python
from bootstrap_retries import python_signatures

DATAPLANE = ROOT / 'experiments/dataplane'
SETUP = RUNTIME / 'dataplane-setup.json'
PROFILES = ('sdk', 'raw-current', 'legacy', 'raw-legacy', 'finsy', 'shell')


def selection(mode, profiles=None, skip_cli=False):
    selected = list(profiles) if profiles is not None else ['sdk', 'legacy'] if mode == 'race' else list(PROFILES)
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(PROFILES):
        raise ValueError('Select each supported profile once')
    if mode == 'race' and not set(selected) <= {'sdk', 'legacy'}:
        raise ValueError('Race mode uses sdk and legacy profiles')
    if mode not in ('smoke', 'race', 'full'):
        raise ValueError('Unknown data-plane mode')
    return dict(run_mode=mode, profiles=selected, repetitions=10 if mode == 'full' else 1,
                cli_repetitions=0 if mode == 'race' or skip_cli else 10 if mode == 'full' else 1,
                race=mode == 'race', skip_cli=skip_cli)


def check_provenance():
    original = json.loads((ROOT / 'data/dataplane/run.json').read_text())['source_sha256']
    records = []
    for name in ('dataplane-source-provenance.json', 'dataplane-runner-provenance.json'):
        record = json.loads((ROOT / 'config' / name).read_text())
        for filename, item in record['files'].items():
            if digest(ROOT / filename) != item['export_sha256']:
                raise ValueError(f'Data-plane source changed: {filename}')
            source = item['source_file'].split('/source/', 1)[1]
            if original[source] != item['source_sha256']:
                raise ValueError(f'Data-plane execution-source binding differs: {filename}')
            if item['unchanged'] and item['export_sha256'] != item['source_sha256']:
                raise ValueError(f'Data-plane source correspondence differs: {filename}')
        records.append(record)
    return records


def input_files():
    selected = set()
    for directory in (DATAPLANE, ROOT / 'scripts/dataplane_checks', ROOT / 'tests/dataplane'):
        selected.update(path for path in directory.rglob('*') if path.is_file() and
                        path.suffix in ('.py', '.go', '.mod', '.sum', '.json') and
                        not any(part in ('__pycache__', 'bin', 'fixtures') for part in path.relative_to(directory).parts))
    selected.update(path for path in (ROOT / 'data/dataplane/fixtures').iterdir() if path.is_file())
    selected.update(ROOT / name for name in ('data/dataplane/plan.json', 'data/dataplane/environment.json',
        'config/dependencies.json', 'config/requirements-finsy.txt', 'config/requirements-shell.txt',
        'config/dataplane-source-provenance.json', 'config/dataplane-runner-provenance.json',
        'scripts/bootstrap_rpc.py', 'scripts/bootstrap_construction.py', 'scripts/bootstrap_retries.py',
        'scripts/bootstrap_dataplane.py', 'scripts/run_dataplane.py', 'scripts/analyze_dataplane.py', 'scripts/verify_dataplane.py'))
    return sorted(selected)


def check_setup():
    check_provenance()
    record = json.loads(SETUP.read_text())
    for field in ('input_sha256', 'generated_sha256'):
        for name, expected in record[field].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f'Data-plane setup changed: {name}. Run bootstrap again.')
    for name, expected in record['source_inventory'].items():
        if inventory(RUNTIME / 'sources' / name) != expected:
            raise ValueError(f'Dependency source changed: {name}')
    if python_signatures() != record['python_signatures']:
        raise ValueError('Data-plane Python environment changed')
    return record


def snapshot_run(output):
    setup = check_setup()
    hashes = {**setup['input_sha256'], **setup['generated_sha256']}
    for name, expected in hashes.items():
        path = output / 'source' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
        if digest(path) != expected:
            raise ValueError(f'Snapshot input changed: {name}')
    return setup, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, help='Optional finsy/ and shell/ wheel directories')
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    if sys.version_info[:2] != (3, 12) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        parser.error('Use CPython 3.12 on Linux x86_64.')
    for name in ('go', 'cc', 'p4c-bm2-ss', 'simple_switch_grpc', 'mn', 'ip'):
        if shutil.which(name) is None:
            parser.error(f'Install {name} and put it on PATH.')
    check_provenance()
    RUNTIME.mkdir(exist_ok=True)
    output = ROOT / 'results/dataplane-setup'
    output.mkdir(parents=True, exist_ok=True)
    log = output / 'bootstrap.log'
    log.write_text('')
    env = environment()
    env.pop('PYTHONOPTIMIZE', None)
    lock = json.loads((ROOT / 'config/dependencies.json').read_text())
    sources = {item['name']: install_source(item) for item in lock['sources']}
    archived = json.loads((ROOT / 'data/dataplane/environment.json').read_text())
    revisions = {item['name']: item['sha'] for item in archived['sources']['sources']}
    revisions['p4runtime-go-controller'] = archived['sdk']['commit']
    if any(revisions[item['name']] != item['commit'] for item in lock['sources']):
        raise ValueError('Pinned clients differ from the archived data-plane environment')
    packages = {name: install_python(name, args.wheelhouse, env, log) for name in ('finsy', 'shell')}
    goroot = command(['go', 'env', 'GOROOT'], ROOT, env, log)
    env['PATH'] = str(Path(goroot) / 'bin') + os.pathsep + env['PATH']
    go = command(['go', 'version'], ROOT, env, log)
    if lock['go_toolchain'] not in go:
        raise ValueError(go)
    command([RUNTIME / 'finsy/bin/python', DATAPLANE / 'compile.py'], ROOT, env, log)
    (DATAPLANE / 'bin').mkdir(exist_ok=True)
    for stack in ('sdk', 'legacy'):
        folder = DATAPLANE / ('go-' + stack)
        command(['go', 'vet', '-mod=readonly', './...'], folder, env, log)
        for race in (False, True):
            binary = DATAPLANE / 'bin' / ('go-' + stack + ('-race' if race else ''))
            command(['go', 'build', '-mod=readonly', *(['-race'] if race else []), '-o', binary, '.'], folder, env, log)
    stamp = datetime.now().astimezone().isoformat()
    commit = archived['sdk']['commit']
    flags = f'-X main.version=v2.0.1 -X main.commit={commit} -X main.date={stamp}'
    command(['go', 'build', '-mod=readonly', '-ldflags', flags, '-o', DATAPLANE / 'bin/p4ctl', './cmd/p4ctl'],
            RUNTIME / 'sources/p4runtime-go-controller', env, log)
    command([DATAPLANE / 'bin/p4ctl', 'version'], ROOT, env, log)
    command([RUNTIME / 'finsy/bin/python', '-m', 'unittest', 'discover', '-s', ROOT / 'tests/dataplane', '-v'], ROOT, env, log)
    generated = sorted(path for name in ('fixtures', 'bin') for path in (DATAPLANE / name).rglob('*') if path.is_file())
    setup = dict(completed_local=stamp, dependencies=lock, source_inventory=sources, python_environments=packages,
                 python_signatures=python_signatures(), go=go, mininet=command(['mn', '--version'], ROOT, env, log),
                 bmv2=command(['simple_switch_grpc', '--version'], ROOT, env, log),
                 p4c=command(['p4c-bm2-ss', '--version'], ROOT, env, log), go_vet_modules=2, binaries=5,
                 input_sha256={str(path.relative_to(ROOT)): digest(path) for path in input_files()},
                 generated_sha256={str(path.relative_to(ROOT)): digest(path) for path in generated})
    SETUP.write_text(json.dumps(setup, indent=2) + '\n')
    print(json.dumps(dict(completed=True, modules=2, binaries=5)))


if __name__ == '__main__':
    main()
