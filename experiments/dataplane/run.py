"""Execute the frozen E5 cases on two isolated Mininet hosts."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import random
import signal
import subprocess
import threading
import time
import traceback
import sys

from common import ACTION, COUNTER, FIELD, REPOSITORY, PROFILES, ROOT, TABLE, Peer, check_frames, driver_command, frame, read_pcap
from control import Observer
from network import Network, Target

sys.path.insert(0, str(REPOSITORY / 'scripts'))
from bootstrap_dataplane import snapshot_run


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def user_options():
    uid = int(os.environ.get('SUDO_UID', '1000'))
    account = pwd.getpwuid(uid)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('P4CTL_', 'SUDO_'))}
    env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    return {'user': uid, 'group': account.pw_gid, 'extra_groups': os.getgrouplist(account.pw_name, account.pw_gid), 'env': env}


class Case:
    def __init__(self, network, directory, trial, profile, race=False):
        self.net, self.directory, self.trial, self.profile, self.race = network, directory, trial, profile, race
        directory.mkdir()
        self.file = (directory / 'events.jsonl').open('w')
        self.trace_lock = threading.Lock()
        self.poller = None
        self.poll_stop = threading.Event()
        self.poll_errors = []
        self.agents, self.driver, self.observer = {}, None, None
        self.target = Target(directory, network.interfaces)
        self.expected_tx, self.expected_rx = [], []
        self.sequence, self.driver_generation = 0, 0
        self.drivers = []

    def trace(self, event, **fields):
        with self.trace_lock:
            self.file.write(json.dumps({'event': event, 'monotonic_ns': time.monotonic_ns(), **fields}) + '\n')
            self.file.flush()

    def start_poll(self):
        self.poll_stop.clear()
        def poll():
            previous = {1: 0, 2: 0}
            try:
                while not self.poll_stop.is_set():
                    start = time.monotonic()
                    for port in (1, 2):
                        row = self.driver.ask('counter', index=port)
                        assert row['packets'] >= previous[port] and row['bytes'] == 64 * row['packets'], row
                        previous[port] = row['packets']
                        self.trace('periodic_counter_sample', generation=self.driver_generation, index=port,
                                   packets=row['packets'], bytes=row['bytes'], interval_seconds=.05)
                    self.poll_stop.wait(max(0, .05 - (time.monotonic() - start)))
            except BaseException as error:
                self.poll_errors.append(repr(error))
        self.poller = threading.Thread(target=poll)
        self.poller.start()

    def stop_poll(self):
        if self.poller is not None:
            self.poll_stop.set()
            self.poller.join(timeout=15)
            assert not self.poller.is_alive()
            self.poller = None
        assert not self.poll_errors, self.poll_errors

    def start(self, cli=False):
        self.agents = self.net.agents(self.directory)
        self.target.start()
        self.observer = Observer(self.target.address, self.trace)
        if not cli:
            self.observer.install()
            self.start_driver()

    def start_driver(self):
        self.driver_generation += 1
        name = 'driver-' + str(self.driver_generation)
        command = driver_command(self.profile, self.target.address)
        if self.race:
            command[0] += '-race'
        def popen(command, **kwargs):
            return subprocess.Popen(command, **kwargs, **user_options())
        self.driver = Peer(command, self.directory, name, popen=popen)
        self.drivers.append(self.driver)
        self.trace('driver_ready', profile=self.profile, generation=self.driver_generation, command=command)

    def stop_driver(self):
        if self.driver is not None:
            self.driver.close()
            self.trace('driver_closed', generation=self.driver_generation, exit_code=self.driver.process.returncode)
            self.driver = None

    def packet(self, source, destination, egress):
        self.sequence += 1
        value = frame(self.trial, self.sequence, source, destination)
        self.expected_tx.append((source, value.hex()))
        if egress is not None:
            self.expected_rx.append((egress, value.hex()))
        return value.hex()

    def send(self, source, payloads):
        self.agents[source].ask('send', frames=payloads, interval_seconds=.001)

    def received(self, port, payload):
        return [r for r in self.agents[port].rows if r.get('kind') == 'rx' and r['payload'] == payload]

    def wait_packets(self):
        for port, payload in self.expected_rx:
            self.agents[port].wait(lambda r: r.get('kind') == 'rx' and r['payload'] == payload, timeout=5)
        time.sleep(.1)
        actual = [(p, r['payload']) for p, agent in self.agents.items() for r in agent.rows if r.get('kind') == 'rx']
        check_frames(self.expected_rx, actual)

    def tables(self, entries):
        actual = self.observer.tables()
        assert actual == sorted(entries, key=lambda e: e['mac']), (actual, entries)

    def counters(self, packets):
        expected = {p: {'packets': packets, 'bytes': 64 * packets} for p in (1, 2)}
        for port in (1, 2):
            value = self.driver.ask('counter', index=port)
            assert {k: value[k] for k in ('packets', 'bytes')} == expected[port], value
        actual = self.observer.counters()
        assert actual == expected, (actual, expected)
        self.trace('counter_checkpoint', expected=expected, actual=actual)

    def traffic(self, entries, frames_per_alias):
        sends = {1: [], 2: []}
        for item in entries:
            port = item['port']
            for _ in range(frames_per_alias):
                sends[3 - port].append(self.packet(3 - port, item['mac'], port))
        for source in (1, 2):
            self.send(source, sends[source])
        self.wait_packets()

    def reactive(self):
        entries = []
        for source, egress in ((1, 2), (2, 1)):
            destination = f'02000000000{egress}'
            payload = self.packet(source, destination, egress)
            self.send(source, [payload])
            packet = self.driver.wait(lambda row: row.get('kind') == 'packet' and row.get('payload') == payload)
            assert packet['ingress_port'] == source
            assert not self.received(egress, payload)
            item = {'mac': destination, 'port': egress}
            self.driver.ask('write', mode='insert', entries=[item])
            entries.append(item)
            self.tables(entries)
            self.driver.ask('send', payload=payload, port=egress)
            self.wait_packets()
            self.trace('first_frame_forwarded', payload=payload, ingress_port=source, egress_port=egress)
        self.traffic(entries, 32)
        self.counters(32)
        packets = [r for r in self.driver.rows if r.get('kind') == 'packet']
        assert len(packets) == 2, packets
        self.trace('reactive_complete', packet_ins=len(packets), first_frames=2, stable_frames=64)

    def counter_recovery(self):
        entries = [{'mac': f'02000000{i:04x}', 'port': i % 2 + 1} for i in range(1, 17)]
        self.driver.ask('write', mode='insert', entries=entries)
        self.tables(entries)
        self.counters(0)
        self.start_poll()
        self.traffic(entries, 8)
        self.counters(64)
        entries = [{'mac': e['mac'], 'port': 3 - e['port']} for e in entries]
        self.driver.ask('write', mode='modify', entries=entries)
        self.tables(entries)
        self.traffic(entries, 8)
        self.counters(128)
        self.stop_poll()
        self.trace('target_restart_begin')
        self.target.stop()
        probe = self.packet(1, entries[0]['mac'], None)
        self.send(1, [probe])
        time.sleep(.1)
        assert not any(self.received(port, probe) for port in (1, 2))
        self.stop_driver()
        self.observer.close()
        self.target.start()
        self.observer = Observer(self.target.address, self.trace)
        self.observer.missing_pipeline()
        self.observer.install()
        self.start_driver()
        self.counters(0)
        self.driver.ask('write', mode='insert', entries=entries)
        self.tables(entries)
        self.start_poll()
        self.traffic(entries, 8)
        self.counters(64)
        self.stop_poll()
        assert not any(row.get('kind') == 'packet' for driver in self.drivers for row in driver.rows)
        self.trace('target_restart_forwarding_restored', restored_entries=len(entries), forwarded_frames=384)

    def cli(self, args):
        command = [str(ROOT / 'bin/p4ctl'), '--config-file', str(ROOT / 'fixtures/empty.yaml'),
                   '--addr', self.target.address, '--device-id', '1', '--election-id', '1', '--insecure=true', '--output=json', *args]
        before = time.monotonic_ns()
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, **user_options())
        after = time.monotonic_ns()
        self.trace('cli_command', command=command, start_monotonic_ns=before, end_monotonic_ns=after,
                   elapsed_ns=after - before, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def cli_case(self):
        self.cli(['version'])
        self.cli(['pipeline', 'set', '--p4info', str(ROOT / 'fixtures/l2.p4info.txt'), '--config', str(ROOT / 'fixtures/l2.json')])
        entries = [{'mac': f'02000000000{port}', 'port': port} for port in (1, 2)]
        for item in entries:
            self.cli(['table', 'insert', '--p4info', str(ROOT / 'fixtures/l2.p4info.txt'), '--table', TABLE,
                      '--match', FIELD + '=0x' + item['mac'], '--action', ACTION, '--param', 'port=' + str(item['port'])])
        self.tables(entries)
        self.traffic(entries, 8)
        for port in (1, 2):
            output = self.cli(['counter', 'read', '--p4info', str(ROOT / 'fixtures/l2.p4info.txt'), '--counter', COUNTER, '--index', str(port)])
            values = json.loads(output)
            assert len(values) == 1 and int(values[0]['packets']) == 8 and int(values[0]['bytes']) == 512, values
        payload = self.packet(1, entries[0]['mac'], 2)
        # PacketOut intentionally chooses port 2 while the installed destination maps to port 1.
        self.expected_tx.pop()
        self.cli(['packet', 'send', '--p4info', str(ROOT / 'fixtures/l2.p4info.txt'), '--port', '2', '--hex', payload])
        self.wait_packets()
        assert self.observer.counters() == {p: {'packets': 8, 'bytes': 512} for p in (1, 2)}
        self.trace('cli_complete', host_frames=16, packet_outs=1)

    def finish(self, success):
        errors = []
        for operation in [self.stop_poll, lambda: self.stop_driver(), self.target.stop,
                          lambda: self.observer.close() if self.observer is not None else None]:
            try:
                operation()
            except BaseException as error:
                errors.append(repr(error))
        for agent in self.agents.values():
            try:
                agent.close(allow_failure=not success)
            except BaseException as error:
                errors.append(repr(error))
        self.file.close()
        if success:
            assert not errors, errors
            tx, rx = [], []
            for port in (1, 2):
                tx.extend((port, value.hex()) for value in read_pcap(self.directory / f'h{port}/tx.pcap'))
                rx.extend((port, value.hex()) for value in read_pcap(self.directory / f'h{port}/rx.pcap'))
            check_frames(self.expected_tx, tx)
            check_frames(self.expected_rx, rx)
            assert all(driver.process.returncode == 0 for driver in self.drivers)
            save(self.directory / 'result.json', {'passed': True, 'trial': self.trial, 'profile': self.profile,
                 'expected_tx': self.expected_tx, 'expected_rx': self.expected_rx, 'captured_tx': len(tx),
                 'captured_rx': len(rx), 'drivers_exited': len(self.drivers), 'target_generations': len(self.target.history),
                 'process_ids': [d.process.pid for d in self.drivers] + [a.process.pid for a in self.agents.values()]})


def cleanup_state(topology):
    interfaces = topology['bmv2_interfaces']
    pids = [item['namespace_pid'] for item in topology['hosts'].values()]
    removed = {name: subprocess.run(['ip', 'link', 'show', 'dev', name], capture_output=True).returncode != 0
               for name in interfaces}
    stopped = {str(pid): not Path('/proc', str(pid)).exists() for pid in pids}
    result = dict(interfaces_removed=removed, namespace_processes_stopped=stopped)
    assert all(removed.values()) and all(stopped.values()), result
    return result


def main():
    from bootstrap_dataplane import selection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('smoke', 'race', 'full'), default='smoke')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES)
    parser.add_argument('--skip-cli', action='store_true')
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    if os.geteuid() != 0:
        parser.error('Use sudo to create the Mininet namespaces and raw Ethernet sockets.')
    configuration = selection(args.mode, args.profiles, args.skip_cli)
    output = args.output.resolve()
    if any(output.is_relative_to(REPOSITORY / name) for name in ('data', 'figures')):
        parser.error('Output must be outside archived data and figures.')
    output.mkdir(parents=True, exist_ok=False)
    setup, hashes = snapshot_run(output)
    plan = json.loads((REPOSITORY / 'data/dataplane/plan.json').read_text())
    manifest = dict(started_utc=datetime.now(timezone.utc).isoformat(), source_sha256=hashes,
                    **configuration, random_seed=plan['random_seed'], timing_basis=plan['timing_basis'],
                    cases=['reactive', 'counter-recovery'], completed=[], stage='starting', pid=os.getpid(), plan=plan)
    save(output / 'manifest.json', manifest)
    save(output / 'environment.json', setup)
    (output / 'schema.json').write_bytes((REPOSITORY / 'data/dataplane/fixtures/schema.json').read_bytes())
    def interrupt(signum, frame):
        raise KeyboardInterrupt('Data-plane experiment interrupted')
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    network = None
    try:
        network = Network()
        topology = network.topology()
        save(output / 'topology.json', topology)
        rng = random.Random(plan['random_seed'])
        schedule = []
        for repetition in range(1, configuration['repetitions'] + 1):
            profiles = list(configuration['profiles'])
            rng.shuffle(profiles)
            schedule.extend((repetition, profile, kind) for profile in profiles for kind in ('reactive', 'counter-recovery'))
        schedule.extend((repetition, 'p4ctl', 'cli') for repetition in range(1, configuration['cli_repetitions'] + 1))
        manifest['schedule'] = schedule
        for trial, (repetition, profile, kind) in enumerate(schedule, 1):
            label = f'{repetition:02}-{profile}-{kind}'
            manifest.update(stage=label, trial=trial)
            save(output / 'manifest.json', manifest)
            case = Case(network, output / label, trial, profile, configuration['race'])
            success = False
            try:
                case.start(cli=kind == 'cli')
                if kind == 'reactive':
                    case.reactive()
                elif kind == 'counter-recovery':
                    case.counter_recovery()
                else:
                    case.cli_case()
                success = True
            finally:
                case.finish(success)
            manifest['completed'].append(label)
            save(output / 'manifest.json', manifest)
            print(json.dumps(dict(completed=label, trial=trial, of=len(schedule))), flush=True)
        manifest.update(stage='sampling_completed', sampling_completed_utc=datetime.now(timezone.utc).isoformat())
    except BaseException as error:
        manifest.update(stage='failed', error=repr(error), traceback=traceback.format_exc())
        raise
    finally:
        try:
            if network is not None:
                network.close()
                save(output / 'network-cleanup.json', cleanup_state(topology))
                manifest['network_cleanup_completed'] = True
        finally:
            save(output / 'manifest.json', manifest)
            files = [path for path in output.rglob('*') if path.is_file() and path.name != 'artifact-hashes.json']
            save(output / 'artifact-hashes.json', {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                                   for path in files})
            uid, gid = int(os.environ.get('SUDO_UID', str(os.getuid()))), int(os.environ.get('SUDO_GID', str(os.getgid())))
            for path in [output, *output.rglob('*')]:
                os.chown(path, uid, gid)


if __name__ == '__main__':
    main()
