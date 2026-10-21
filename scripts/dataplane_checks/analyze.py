"""Original E5 expected traffic and per-case evidence checks."""

from collections import defaultdict
import json

from .model import check_frames, frame, read_pcap


def expected_frames(trial, kind):
    tx, rx, sequence = [], [], 0
    def add(source, destination, egress, host=True):
        nonlocal sequence
        sequence += 1
        payload = frame(trial, sequence, source, destination).hex()
        if host:
            tx.append((source, payload))
        if egress is not None:
            rx.append((egress, payload))
    if kind == 'reactive':
        add(1, '020000000002', 2)
        add(2, '020000000001', 1)
        for port in (2, 1):
            for _ in range(32):
                add(3 - port, f'02000000000{port}', port)
    elif kind == 'counter-recovery':
        for epoch in range(3):
            if epoch == 2:
                add(1, '020000000001', None)
            for number in range(1, 17):
                port = number % 2 + 1
                if epoch:
                    port = 3 - port
                for _ in range(8):
                    add(3 - port, f'02000000{number:04x}', port)
    elif kind == 'cli':
        for port in (1, 2):
            for _ in range(8):
                add(3 - port, f'02000000000{port}', port)
        add(1, '020000000001', 2, host=False)
    else:
        raise ValueError(kind)
    return tx, rx


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def verify_case(directory, trial, profile, kind):
    result = json.loads((directory / 'result.json').read_text())
    assert result['passed'] and result['trial'] == trial and result['profile'] == profile
    tx, rx = expected_frames(trial, kind)
    check_frames(tx, [tuple(v) for v in result['expected_tx']])
    check_frames(rx, [tuple(v) for v in result['expected_rx']])
    actual_tx, actual_rx = [], []
    for port in (1, 2):
        actual_tx += [(port, value.hex()) for value in read_pcap(directory / f'h{port}/tx.pcap')]
        actual_rx += [(port, value.hex()) for value in read_pcap(directory / f'h{port}/rx.pcap')]
    check_frames(tx, actual_tx)
    check_frames(rx, actual_rx)
    assert result['captured_tx'] == len(tx) and result['captured_rx'] == len(rx)
    events = rows(directory / 'events.jsonl')
    checkpoints = [r for r in events if r['event'] == 'counter_checkpoint']
    expected = [32] if kind == 'reactive' else [0, 64, 128, 0, 64] if kind == 'counter-recovery' else []
    assert len(checkpoints) == len(expected)
    for row, packets in zip(checkpoints, expected):
        target = {str(p): {'packets': packets, 'bytes': packets * 64} for p in (1, 2)}
        assert row['expected'] == row['actual'] == target
    periodic = [r for r in events if r['event'] == 'periodic_counter_sample']
    if kind == 'counter-recovery':
        assert periodic and {r['generation'] for r in periodic} == {1, 2}
        previous = defaultdict(int)
        for row in periodic:
            key = row['generation'], row['index']
            assert previous[key] <= row['packets'] <= (128 if row['generation'] == 1 else 64)
            assert row['bytes'] == 64 * row['packets']
            previous[key] = row['packets']
        assert any(r['event'] == 'pipeline_missing' for r in events)
        assert any(r['event'] == 'target_restart_forwarding_restored' for r in events)
    packets = [r for path in directory.glob('driver-*.jsonl') for r in rows(path) if r.get('kind') == 'packet']
    if kind == 'reactive':
        assert [(r['payload'], r['ingress_port']) for r in packets] == [(tx[0][1], 1), (tx[1][1], 2)]
    else:
        assert not packets
    for path in directory.glob('driver-*.jsonl'):
        assert all(r.get('ok', True) for r in rows(path))
    for path in directory.glob('driver-*.stderr'):
        assert 'DATA RACE' not in path.read_text()
    targets = json.loads((directory / 'target-processes.json').read_text())
    assert len(targets) == (2 if kind == 'counter-recovery' else 1)
    assert all('exit_code' in item for item in targets)
    calls = [r for r in events if r['event'] == 'cli_command']
    if kind == 'cli':
        assert len(calls) == 7 and all(r['exit_code'] == 0 for r in calls)
        assert any('v2.0.1' in r['stdout'] for r in calls if r['command'][-1] == 'version')
    return {'profile': profile, 'case': kind, 'trial': trial, 'host_frames_sent': len(tx), 'received_frames': len(rx),
            'expected_drops': len(tx) - len(rx) if kind != 'cli' else 0,
            'packet_ins': len(packets), 'counter_checkpoints': len(checkpoints), 'periodic_counter_samples': len(periodic),
            'target_restarts': len(targets) - 1, 'controller_processes_exited': result['drivers_exited'],
            'cli_invocations': len(calls)}
