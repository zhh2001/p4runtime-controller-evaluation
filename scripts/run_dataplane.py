"""Run and verify the Mininet/BMv2 functional cases."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess

from bootstrap_dataplane import ROOT, RUNTIME, DATAPLANE, PROFILES, selection, check_setup, environment


def run_logged(argv, env, log):
    with log.open('w') as stream:
        process = subprocess.Popen([str(arg) for arg in argv], cwd=ROOT, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, argv)
        except BaseException:
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('smoke', 'race', 'full'), default='smoke')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES)
    parser.add_argument('--skip-cli', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    try:
        configuration = selection(args.mode, args.profiles, args.skip_cli)
    except ValueError as error:
        parser.error(str(error))
    if os.geteuid() != 0:
        parser.error('Use sudo to create the Mininet namespaces and raw Ethernet sockets.')
    output = (args.output or ROOT / 'results' / ('dataplane-' + args.mode + '-' + datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
    if any(output.is_relative_to(ROOT / name) for name in ('data', 'figures')):
        parser.error('Output must be outside archived data and figures.')
    setup = check_setup()
    output.mkdir(parents=True, exist_ok=False)
    record = dict(started_local=datetime.now().astimezone().isoformat(), configuration=configuration, setup=setup, completed=False)
    env = environment()
    env.pop('PYTHONOPTIMIZE', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    def interrupt(signum, frame):
        raise KeyboardInterrupt('Data-plane launcher interrupted')
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        argv = [RUNTIME / 'finsy/bin/python', DATAPLANE / 'run.py', '--mode', args.mode, '--output', output / 'run']
        if args.profiles:
            argv += ['--profiles', *args.profiles]
        if args.skip_cli:
            argv.append('--skip-cli')
        run_logged(argv, env, output / 'run.log')
        run_logged([RUNTIME / 'finsy/bin/python', ROOT / 'scripts/analyze_dataplane.py', output / 'run'], env, output / 'analysis.log')
        check_setup()
        analysis = json.loads((output / 'run/verification.json').read_text())
        record.update(completed=True, completed_local=datetime.now().astimezone().isoformat(), analysis=analysis)
    except BaseException as error:
        record['failure'] = dict(type=type(error).__name__, message=str(error))
        raise
    finally:
        (output / 'execution.json').write_text(json.dumps(record, indent=2) + '\n')
        uid, gid = int(os.environ.get('SUDO_UID', str(os.getuid()))), int(os.environ.get('SUDO_GID', str(os.getgid())))
        for path in [output, *output.rglob('*')]:
            os.chown(path, uid, gid)
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(dict(completed=True, cases=analysis['cases'], output=str(output))))


if __name__ == '__main__':
    main()
