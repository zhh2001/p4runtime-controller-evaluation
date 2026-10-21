"""Fixed application workloads and a retry policy independent of the RPC relay."""

import base64
import copy


PROFILES = ['sdk', 'raw-current', 'legacy', 'raw-legacy', 'finsy', 'shell']


def operation(index, kind='INSERT'):
    return dict(original_index=index, key=index+1, type=kind, port=index+11,
                metadata=f'desired-{index}')


def cases(mode):
    if mode == 'controlled':
        definitions = [
            ('all-ok', [0, 0, 0], 'normal'),
            ('transient-first', [14, 0, 0, 0, 0], 'normal'),
            ('transient-rebased', [0, 14, 0, 8, 0], 'normal'),
            ('all-transient', [14, 8, 14], 'normal'),
            ('permanent-error', [0, 3, 0], 'normal'),
            ('short-details', [0, 14, 0], 'short'),
            ('wrong-detail-type', [0, 14, 0], 'wrong_type'),
            ('missing-details', [0, 14, 0], 'missing'),
            ('transport-partial', [0, 14, 0], 'transport'),
            ('transport-complete', [0, 0, 0], 'transport'),
        ]
        result = []
        for name, codes, response in definitions:
            schedule = [codes]
            if name == 'transient-rebased': schedule += [[0, 14], [0]]
            elif name == 'all-transient': schedule += [[0, 0, 14], [0]]
            else: schedule += [[0] * max(1, sum(c != 0 for c in codes))]
            result.append(dict(id=name, updates=[operation(i) for i in range(len(codes))],
                               seeds=[], schedule=schedule, response=response))
        return result
    result = []
    for name, seed_keys, kinds in [
        ('all-ok', [], ['INSERT']*5),
        ('duplicate-middle', [3], ['INSERT']*5),
        ('mixed-conflicts', [2, 5], ['INSERT', 'INSERT', 'MODIFY', 'DELETE', 'MODIFY']),
        ('all-duplicates', [1, 2, 3], ['INSERT']*3),
        ('missing-delete', [1, 3], ['DELETE']*3),
        ('single-duplicate', [1], ['INSERT']),
    ]:
        result.append(dict(id=name, updates=[operation(i, kind) for i, kind in enumerate(kinds)],
                           seeds=[dict(key=k, port=1, metadata='seed') for k in seed_keys],
                           response='normal'))
    return result


def semantic(entry):
    value = entry.get('table_entry', entry)
    if value.get('is_default_action') or not value.get('match'): return None
    key = int.from_bytes(base64.b64decode(value['match'][0]['exact']['value']), 'big')
    action = value['action']['action']
    port = int.from_bytes(base64.b64decode(action['params'][0]['value']), 'big')
    return dict(key=key, port=port,
                metadata=base64.b64decode(value.get('metadata', '')).decode())


def state_map(entities):
    result = [semantic(e) for e in entities]
    return {v['key']:v for v in result if v is not None}


def reconcile(updates, state):
    """Produce only operations whose desired final state is not already present."""
    result = []
    for original in updates:
        op = copy.deepcopy(original)
        current = state.get(op['key'])
        if op['type'] == 'DELETE':
            if current is not None: result.append(op)
        else:
            desired = {k:op[k] for k in ('key', 'port', 'metadata')}
            if current != desired:
                op['type'] = 'MODIFY' if current is not None else 'INSERT'
                result.append(op)
    return result


def retry_plan(pending, codes, state=None):
    """Transient retries preserve bytes. Conflicts use observed desired state."""
    if codes is None:
        if state is None: return dict(read_required=True)
        return dict(pending=reconcile(pending, state), blocked=[])
    failed = [(op, code) for op, code in zip(pending, codes) if code]
    if any(code in (5, 6) for _, code in failed) and state is None:
        return dict(read_required=True)
    again, blocked = [], []
    for op, code in failed:
        if code in (8, 14): again.append(copy.deepcopy(op))
        elif code in (5, 6): again += reconcile([op], state)
        else: blocked.append(op['original_index'])
    return dict(pending=again, blocked=blocked)
