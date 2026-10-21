"""Independent stdlib checks of E5 PCAPs, host traffic and controller state."""

import argparse
import base64
from collections import Counter, defaultdict
import json
from pathlib import Path


def records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def capture(path):
    data = path.read_bytes()
    assert data[:24] == bytes.fromhex('d4c3b2a1020004000000000000000000ffff000001000000'), 'PCAP header'
    offset, frames = 24, []
    while offset < len(data):
        assert offset + 16 <= len(data), 'PCAP record header'
        size = int.from_bytes(data[offset + 8:offset + 12], 'little')
        original = int.from_bytes(data[offset + 12:offset + 16], 'little')
        offset += 16
        assert size == original == 64 and offset + size <= len(data), 'PCAP frame length'
        value = data[offset:offset + size]
        assert value[12:22] == bytes.fromhex('88b5') + b'E5PKT001' and value[30:] == b'\xa5' * 34, 'Frame payload'
        frames.append(value.hex())
        offset += size
    return frames


def workload(trial, kind):
    tx, rx = [], []
    sequence = 0
    def packet(source, destination, output, host=True):
        nonlocal sequence
        sequence += 1
        raw = (bytes.fromhex(destination) + bytes([2, 0, 0, 0, 0, source]) + b'\x88\xb5E5PKT001' +
               trial.to_bytes(4, 'big') + sequence.to_bytes(4, 'big') + b'\xa5' * 34).hex()
        if host:
            tx.append((source, raw))
        if output:
            rx.append((output, raw))
    if kind == 'reactive':
        for source in (1, 2):
            packet(source, f'02000000000{3 - source}', 3 - source)
        for output in (2, 1):
            for _ in range(32):
                packet(3 - output, f'02000000000{output}', output)
    elif kind == 'counter-recovery':
        for epoch in range(3):
            if epoch == 2:
                packet(1, '020000000001', None)
            for address in range(1, 17):
                output = address % 2 + 1 if epoch == 0 else 2 - address % 2
                for _ in range(8):
                    packet(3 - output, f'02000000{address:04x}', output)
    else:
        assert kind == 'cli'
        for output in (1, 2):
            for _ in range(8):
                packet(3 - output, f'02000000000{output}', output)
        packet(1, '020000000001', 2, host=False)
    return tx, rx


def protocol(directory, name):
    commands = json.loads((directory / (name + '-commands.json')).read_text())
    output = records(directory / (name + '.jsonl'))
    assert output[0]['kind'] == 'ready'
    assert [command['id'] for command in commands] == list(range(1, len(commands) + 1)), 'Command IDs'
    replies = [row for row in output if row['kind'] == 'reply']
    assert [row['id'] for row in replies] == [command['id'] for command in commands], 'Reply IDs'
    assert all(row['ok'] for row in replies) and commands[-1]['op'] == 'close', 'Peer completion'
    assert (directory / (name + '.stderr')).read_text().find('DATA RACE') == -1
    return commands, output, replies


def table_entries(event, schema):
    result = []
    for response in event['protobuf']:
        for entity in response.get('entities', []):
            entry = entity['table_entry']
            if entry.get('is_default_action'):
                continue
            assert entry['table_id'] == schema['table']['id'] and len(entry['match']) == 1
            field = entry['match'][0]
            assert field['field_id'] == schema['table']['field_id'] and set(field) == {'field_id', 'exact'}
            mac = base64.b64decode(field['exact']['value'], validate=True)
            assert 0 < len(mac) <= 6
            action = entry['action']['action']
            assert action['action_id'] == schema['action']['id'] and len(action['params']) == 1
            param = action['params'][0]
            assert param['param_id'] == schema['action']['param_id']
            port = int.from_bytes(base64.b64decode(param['value'], validate=True), 'big')
            assert port in (1, 2)
            result.append(dict(mac=f'{int.from_bytes(mac, "big"):012x}', port=port))
    assert sorted(result, key=lambda item: item['mac']) == sorted(event['entries'], key=lambda item: item['mac']), 'Raw table read'
    return sorted(result, key=lambda item: item['mac'])


def verify_case(directory, trial, profile, kind, schema):
    tx, rx = workload(trial, kind)
    for port in (1, 2):
        commands, output, _ = protocol(directory, 'host' + str(port))
        for direction, expected in (('tx', tx), ('rx', rx)):
            observed = capture(directory / f'h{port}/{direction}.pcap')
            assert Counter(observed) == Counter(payload for endpoint, payload in expected if endpoint == port), 'PCAP port or content'
            assert Counter(observed) == Counter(row['payload'] for row in output if row['kind'] == direction), 'Host log versus PCAP'
        sent = [payload for command in commands if command['op'] == 'send' for payload in command['frames']]
        assert Counter(sent) == Counter(payload for endpoint, payload in tx if endpoint == port), 'Host send commands'
        assert all(command['interval_seconds'] == .001 for command in commands if command['op'] == 'send')
    events = records(directory / 'events.jsonl')
    assert [row['monotonic_ns'] for row in events] == sorted(row['monotonic_ns'] for row in events), 'Event ordering'
    initial = [dict(mac=f'02000000{number:04x}', port=number % 2 + 1) for number in range(1, 17)]
    swapped = [dict(mac=item['mac'], port=3 - item['port']) for item in initial]
    expected_tables = ([initial, swapped, swapped] if kind == 'counter-recovery' else
                       [[dict(mac='020000000002', port=2)], [dict(mac='020000000001', port=1), dict(mac='020000000002', port=2)]]
                       if kind == 'reactive' else [[dict(mac='020000000001', port=1), dict(mac='020000000002', port=2)]])
    tables = [table_entries(event, schema) for event in events if event['event'] == 'table_read']
    assert tables == expected_tables, 'Installed table state'
    checkpoints = [row for row in events if row['event'] == 'counter_checkpoint']
    packet_counts = [0, 64, 128, 0, 64] if kind == 'counter-recovery' else [32] if kind == 'reactive' else []
    expected_counters = [{str(port): dict(packets=count, bytes=64 * count) for port in (1, 2)} for count in packet_counts]
    assert [row['actual'] for row in checkpoints] == [row['expected'] for row in checkpoints] == expected_counters
    independent_reads = [row['values'] for row in events if row['event'] == 'counter_read']
    assert independent_reads == (expected_counters if kind != 'cli' else [{str(port): dict(packets=8, bytes=512) for port in (1, 2)}]), 'Independent counter reads'
    generations = 2 if kind == 'counter-recovery' else 1 if kind == 'reactive' else 0
    driver_counters, packets = Counter(), []
    for generation in range(1, generations + 1):
        commands, output, replies = protocol(directory, 'driver-' + str(generation))
        packets += [row for row in output if row['kind'] == 'packet']
        writes = [(command['mode'], command['entries']) for command in commands if command['op'] == 'write']
        if kind == 'reactive':
            assert writes == [('insert', [dict(mac='020000000002', port=2)]), ('insert', [dict(mac='020000000001', port=1)])]
            sends = [(command['port'], command['payload']) for command in commands if command['op'] == 'send']
            assert sends == rx[:2], 'PacketOut explicit ports'
        else:
            assert writes == ([('insert', initial), ('modify', swapped)] if generation == 1 else [('insert', swapped)]), 'Batch write intent'
            assert not any(command['op'] == 'send' for command in commands)
        for command, reply in zip(commands, replies):
            if command['op'] == 'counter':
                driver_counters[generation, command['index'], reply['packets'], reply['bytes']] += 1
    if kind == 'reactive':
        assert [(row['ingress_port'], row['payload']) for row in packets] == tx[:2]
    else:
        assert not packets
    samples = [row for row in events if row['event'] == 'periodic_counter_sample']
    accounted = Counter()
    previous = defaultdict(int)
    for row in samples:
        generation, port = row['generation'], row['index']
        assert generation in (1, 2) and port in (1, 2) and row['interval_seconds'] == .05
        assert previous[generation, port] <= row['packets'] <= (128 if generation == 1 else 64)
        assert row['bytes'] == row['packets'] * 64
        previous[generation, port] = row['packets']
        accounted[generation, port, row['packets'], row['bytes']] += 1
    for index, count in enumerate(packet_counts):
        generation = 2 if kind == 'counter-recovery' and index >= 3 else 1
        for port in (1, 2):
            accounted[generation, port, count, 64 * count] += 1
    assert accounted == driver_counters, 'Counter samples versus raw driver replies'
    if kind == 'counter-recovery':
        assert {row['generation'] for row in samples} == {1, 2}
        names = [row['event'] for row in events]
        assert names.index('target_restart_begin') < names.index('pipeline_missing') < names.index('target_restart_forwarding_restored')
        missing = next(row for row in events if row['event'] == 'pipeline_missing')
        assert missing['rpc_code'] in (0, 9)
        if missing['rpc_code'] == 0:
            assert not missing.get('protobuf', {}).get('config', {}).get('p4info', {}).get('tables')
        restored = next(row for row in events if row['event'] == 'target_restart_forwarding_restored')
        assert restored['restored_entries'] == 16 and restored['forwarded_frames'] == 384
    else:
        assert not samples
    ready = [row for row in events if row['event'] == 'driver_ready']
    closed = [row for row in events if row['event'] == 'driver_closed']
    assert [row['generation'] for row in ready] == [row['generation'] for row in closed] == list(range(1, generations + 1))
    assert all(row['profile'] == profile for row in ready) and all(row['exit_code'] == 0 for row in closed)
    targets = json.loads((directory / 'target-processes.json').read_text())
    assert len(targets) == (2 if kind == 'counter-recovery' else 1)
    assert [row['generation'] for row in targets] == list(range(1, len(targets) + 1))
    for target in targets:
        assert target['exit_code'] == -15 and target['start_monotonic_ns'] < target['stop_monotonic_ns']
        command = target['command']
        assert command[command.index('--device-id') + 1] == '1' and command[command.index('--cpu-port') + 1] == '255'
        interfaces = [command[i + 1] for i, value in enumerate(command) if value == '-i']
        assert len(interfaces) == 2 and [value.split('@')[0] for value in interfaces] == ['1', '2']
    if len(targets) == 2:
        assert targets[0]['stop_monotonic_ns'] < targets[1]['start_monotonic_ns']
    calls = [row for row in events if row['event'] == 'cli_command']
    assert len(calls) == (7 if kind == 'cli' else 0)
    if calls:
        expected_actions = [('version',), ('pipeline', 'set'), ('table', 'insert'), ('table', 'insert'),
                            ('counter', 'read'), ('counter', 'read'), ('packet', 'send')]
        for call, action in zip(calls, expected_actions):
            command = call['command']
            assert tuple(command[11:11 + len(action)]) == action, 'CLI action'
            assert call['exit_code'] == 0 and call['elapsed_ns'] == call['end_monotonic_ns'] - call['start_monotonic_ns'] > 0
        assert 'v2.0.1' in calls[0]['stdout']
        for port, call in enumerate(calls[4:6], 1):
            output = json.loads(call['stdout'])
            assert len(output) == 1 and output[0] == dict(name=schema['counter']['name'], id=schema['counter']['id'],
                                                        index=str(port), packets='8', bytes='512')
        command = calls[-1]['command']
        assert command[command.index('--port') + 1] == '2' and command[command.index('--hex') + 1] == rx[-1][1]
    result = json.loads((directory / 'result.json').read_text())
    assert result['passed'] and (result['trial'], result['profile']) == (trial, profile)
    assert result['captured_tx'] == len(tx) and result['captured_rx'] == len(rx)
    assert Counter(map(tuple, result['expected_tx'])) == Counter(tx) and Counter(map(tuple, result['expected_rx'])) == Counter(rx)
    assert result['drivers_exited'] == generations and result['target_generations'] == len(targets)
    assert len(set(result['process_ids'])) == len(result['process_ids']) == generations + 2
    return dict(profile=profile, case=kind, trial=trial, host_frames_sent=len(tx), received_frames=len(rx),
                expected_drops=1 if kind == 'counter-recovery' else 0, packet_ins=len(packets),
                counter_checkpoints=len(checkpoints), periodic_counter_samples=len(samples),
                target_restarts=len(targets) - 1, controller_processes_exited=generations, cli_invocations=len(calls))


def verify(output):
    manifest = json.loads((output / 'manifest.json').read_text())
    schema = json.loads((output / 'schema.json').read_text())
    checked = []
    for trial, (repetition, profile, kind) in enumerate(manifest['schedule'], 1):
        label = f'{repetition:02}-{profile}-{kind}'
        checked.append(verify_case(output / label, trial, profile, kind, schema))
    totals = {key: sum(row[key] for row in checked) for key in checked[0] if key not in ('profile', 'case', 'trial')}
    result = dict(cases=len(checked), pcap_files=4 * len(checked), totals=totals, trials=checked)
    (output / 'independent-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(verified=True, cases=len(checked), totals=totals)))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error('Run without -O so evidence checks remain enabled.')
    verify(args.output.resolve())
