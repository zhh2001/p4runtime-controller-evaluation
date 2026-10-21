"""Two Mininet hosts and an owned BMv2 process."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import grpc
from common import ROOT


class Network:
    def __init__(self):
        sys.path.append('/usr/lib/python3/dist-packages')
        from mininet.net import Mininet
        from mininet.log import setLogLevel
        setLogLevel('warning')
        tag = uuid.uuid4().hex[:6]
        self.net = Mininet(controller=None, build=False)
        self.hosts = {}
        self.interfaces = []
        try:
            self.switch = self.net.addHost('e5w' + tag, inNamespace=False, ip=None)
            for port in (1, 2):
                host = self.net.addHost(f'e5{port}' + tag, ip=None, mac=f'02:00:00:00:00:0{port}')
                link = self.net.addLink(host, self.switch)
                self.hosts[port] = host
                self.interfaces.append(link.intf2.name)
            self.net.build()
            for host in self.hosts.values():
                host.cmd('sysctl -qw net.ipv6.conf.all.disable_ipv6=1')
                host.cmd('ip link set dev', host.defaultIntf().name, 'promisc on')
        except BaseException:
            self.net.stop()
            raise

    def topology(self):
        return {'hosts': {port: {'name': host.name, 'namespace_pid': host.pid, 'interface': host.defaultIntf().name}
                          for port, host in self.hosts.items()}, 'bmv2_interfaces': self.interfaces}

    def agents(self, directory):
        from common import Peer
        agents = {}
        try:
            for port, host in self.hosts.items():
                output = directory / f'h{port}'
                output.mkdir()
                command = ['/usr/bin/python3', str(ROOT / 'host.py'), '--interface', host.defaultIntf().name,
                           '--directory', str(output)]
                agents[port] = Peer(command, directory, f'host{port}', popen=host.popen)
            return agents
        except BaseException:
            for peer in agents.values():
                peer.close(allow_failure=True)
            raise

    def close(self):
        self.net.stop()


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Target:
    def __init__(self, directory, interfaces):
        self.directory, self.interfaces = directory, interfaces
        self.port, self.thrift = unused_port(), unused_port()
        while self.thrift == self.port:
            self.thrift = unused_port()
        self.address = f'127.0.0.1:{self.port}'
        self.process, self.history = None, []

    def save(self):
        (self.directory / 'target-processes.json').write_text(json.dumps(self.history, indent=2) + '\n')

    def start(self):
        assert self.process is None
        generation = len(self.history) + 1
        self.ipc = Path(f'/tmp/p4e5-{self.port}-{generation}.ipc')
        command = ['simple_switch_grpc', '--no-p4', '--device-id', '1', '--thrift-port', str(self.thrift),
                   '--notifications-addr', 'ipc://' + str(self.ipc), '--log-console', '-L', 'info']
        for port, name in enumerate(self.interfaces, 1):
            command += ['-i', f'{port}@{name}']
        command += ['--', '--grpc-server-addr', self.address, '--cpu-port', '255']
        self.log = (self.directory / f'target-{generation}.log').open('w')
        try:
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        except BaseException:
            self.log.close()
            raise
        self.history.append({'generation': generation, 'pid': self.process.pid, 'command': command,
                             'start_monotonic_ns': time.monotonic_ns()})
        self.save()
        channel = grpc.insecure_channel(self.address)
        try:
            grpc.channel_ready_future(channel).result(timeout=10)
            assert self.process.poll() is None
        except BaseException:
            self.stop()
            raise
        finally:
            channel.close()

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.history[-1].update(stop_monotonic_ns=time.monotonic_ns(), exit_code=self.process.returncode)
        self.process = None
        self.log.close()
        self.ipc.unlink(missing_ok=True)
        self.save()
