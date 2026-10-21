"""Shared compiled program and frozen table-constraint inputs."""

import json
from pathlib import Path
import subprocess

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parent


def build():
    folder = ROOT / 'fixtures'
    folder.mkdir(exist_ok=True)
    source = r'''#include <core.p4>
#include <v1model.p4>
header key_t { bit<8> x; bit<8> y; }
struct headers_t { key_t key; }
struct metadata_t { bit<8> tag; }
parser MyParser(packet_in p, out headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    state start { p.extract(h.key); transition accept; }
}
control Verify(inout headers_t h, inout metadata_t m) { apply {} }
control Compute(inout headers_t h, inout metadata_t m) { apply {} }
control MyIngress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    action common(bit<9> port) { s.egress_spec = port; }
    action table_action(bit<9> port) { s.egress_spec = port; }
    action default_action(bit<9> port) { s.egress_spec = port; }
    action sparse_action(@id(19) bit<9> port, @id(7) bit<8> tag) { s.egress_spec = port; m.tag = tag; }
    table direct {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
    }
    table scoped {
        key = { h.key.x: exact; }
        actions = { common; @tableonly table_action; @defaultonly default_action; NoAction; }
        default_action = common(0); size = 16;
    }
    table const_default {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        const default_action = common(0); size = 16;
    }
    table const_entries {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
        const entries = { 1: common(1); }
    }
    action_profile(16) profile;
    table indirect {
        key = { h.key.x: exact; }
        actions = { common; }
        implementation = profile; size = 16;
    }
    action_selector(HashAlgorithm.crc32, 16, 8) selector;
    table selected {
        key = { h.key.x: exact; h.key.y: selector; }
        actions = { common; }
        implementation = selector; size = 16;
    }
    table idle {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); support_timeout = true; size = 16;
    }
    table sparse {
        key = { h.key.x: exact @id(13); h.key.y: exact @id(5); }
        actions = { sparse_action; NoAction; }
        default_action = NoAction(); size = 16;
    }
    table ternary_table {
        key = { h.key.x: ternary; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
    }
    apply {
        direct.apply(); scoped.apply(); const_default.apply(); const_entries.apply();
        indirect.apply(); selected.apply(); idle.apply(); sparse.apply(); ternary_table.apply();
    }
}
control MyEgress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) { apply {} }
control MyDeparser(packet_out p, in headers_t h) { apply { p.emit(h.key); } }
V1Switch(MyParser(), Verify(), MyIngress(), MyEgress(), Compute(), MyDeparser()) main;
'''
    (folder/'tables.p4').write_text(source)
    result = subprocess.run(['p4c-bm2-ss', '--p4v', '16', '--p4runtime-files', 'compiler.p4info.txt',
                             '-o', 'tables.json', 'tables.p4'], cwd=folder, capture_output=True, text=True)
    (folder/'compiler.log').write_text(result.stdout+result.stderr)
    assert result.returncode == 0, result.stderr
    info = text_format.Parse((folder/'compiler.p4info.txt').read_text(), p4i.P4Info())
    for table in info.tables:
        table.ClearField('initial_default_action')
        table.ClearField('has_initial_entries')
    (folder/'target.p4info.txt').write_text(text_format.MessageToString(info))
    tables = {t.preamble.alias:t for t in info.tables}
    assert len(tables) == 9
    assert tables['const_entries'].is_const_table and tables['const_default'].const_default_action_id
    assert tables['idle'].idle_timeout_behavior == p4i.Table.NOTIFY_CONTROL
    assert [f.id for f in tables['sparse'].match_fields] == [13, 5]
    sparse = next(a for a in info.actions if a.preamble.alias == 'sparse_action')
    assert [p.id for p in sparse.params] == [19, 7]
    assert {r.scope for r in tables['scoped'].action_refs} == {0, 1, 2}
    schema=dict(tables={t.preamble.name:dict(id=t.preamble.id,fields={f.name:dict(id=f.id,width=f.bitwidth,kind=f.match_type) for f in t.match_fields},
                                           implementation=t.implementation_id,const=t.is_const_table,const_default=t.const_default_action_id,
                                           idle=t.idle_timeout_behavior,actions={str(r.id):r.scope for r in t.action_refs}) for t in info.tables},
                actions={a.preamble.name:dict(id=a.preamble.id,params={p.name:dict(id=p.id,width=p.bitwidth) for p in a.params}) for a in info.actions})
    (folder/'schema.json').write_text(json.dumps(schema,indent=2)+'\n')
    cases = []

    def add(name, family, category='valid', table='direct', **kw):
        c = dict(id=name, family=family, category=category, op='write', update='MODIFY',
                 table='MyIngress.'+table, default=False, priority=0, ttl=0,
                 matches=[dict(name='h.key.x', value='01', kind='exact')],
                 action_kind='direct', action='MyIngress.common', params=[dict(name='port', value='07')])
        c.update(kw)
        cases.append(c)

    for label in ('modify', 'reset', 'insert', 'delete', 'stale-match', 'stale-priority'):
        fields = dict(default=True, matches=[])
        cat = 'valid'
        if label == 'reset': fields.update(action_kind='none', params=[])
        if label in ('insert', 'delete'):
            fields['update'] = label.upper(); cat = 'invalid'
        if label == 'stale-match': fields['matches']=[dict(name='h.key.x',value='01',kind='exact')]; cat='policy'
        if label == 'stale-priority': fields['priority']=10; cat='policy'
        add('default-'+label, 'default', cat, **fields)
    add('ordinary-insert', 'ordinary', update='INSERT', matches=[dict(name='h.key.x',value='02',kind='exact')])
    add('ordinary-modify', 'ordinary')
    add('ordinary-delete', 'ordinary', update='DELETE', action_kind='none')
    add('delete-ignored-action-ttl', 'ordinary', update='DELETE', ttl=-1,
        action='MyIngress.default_action', params=[dict(name='port',value='ffff')])
    add('ordinary-missing-action', 'ordinary', 'invalid', action_kind='none', params=[])
    add('ordinary-exact-priority', 'priority', 'invalid', priority=10)
    add('ordinary-negative-priority', 'priority', 'invalid', priority=-1)
    for label in ('ordinary', 'same-action', 'other-action', 'reset'):
        add('constant-default-'+label, 'constant_default', 'valid' if label=='ordinary' else 'invalid',
            table='const_default', default=label!='ordinary', matches=[] if label!='ordinary' else [dict(name='h.key.x',value='01',kind='exact')],
            action_kind='none' if label=='reset' else 'direct',
            action='NoAction' if label=='other-action' else 'MyIngress.common', params=[] if label in ('reset','other-action') else [dict(name='port',value='00')])
    for operation in ('MODIFY','INSERT','DELETE'):
        add('constant-entry-'+operation.lower(), 'constant_table', 'invalid', table='const_entries', update=operation,
            matches=[dict(name='h.key.x',value='02' if operation=='INSERT' else '01',kind='exact')])
    add('constant-table-default-modify', 'constant_table', table='const_entries', default=True, matches=[])
    add('constant-table-default-reset', 'constant_table', table='const_entries', default=True, matches=[], action_kind='none')
    for action, mode, valid in (('table_action',False,True),('table_action',True,False),('default_action',True,True),
                                ('default_action',False,False),('common',False,True),('common',True,True)):
        add(f'scope-{action}-'+('default' if mode else 'ordinary'), 'action_scope', 'valid' if valid else 'invalid', table='scoped',
            default=mode, matches=[] if mode else [dict(name='h.key.x',value='01',kind='exact')], action='MyIngress.'+action)
    add('action-not-in-table', 'action_scope', 'invalid', action='MyIngress.default_action')
    for table, kinds in (('indirect',('member','group','direct')),('selected',('member','group'))):
        for kind in kinds:
            for ref in ((1,0,99) if kind in ('member','group') else (1,)):
                valid = ref==1 and (kind=='member' or table=='selected' and kind=='group')
                add(f'{table}-{kind}-{ref}', 'indirect', 'valid' if valid else 'invalid', table=table, action_kind=kind, reference=ref)
    for kind in ('member','group'):
        add('direct-with-'+kind, 'indirect', 'invalid', action_kind=kind, reference=1)
    for table in ('direct','idle'):
        for ttl in (0,-1,1,60000000000):
            add(f'ttl-{table}-{ttl}', 'idle_timeout', 'valid' if ttl==0 or table=='idle' and ttl>0 else 'invalid', table=table, ttl=ttl)
    for ttl in (0,-1,1):
        add(f'ttl-default-{ttl}', 'idle_timeout', 'valid' if ttl==0 else 'invalid', table='idle', default=True, matches=[], ttl=ttl)
    for priority in (10,0,-1):
        add('ternary-priority-'+str(priority), 'priority', 'valid' if priority>0 else 'invalid', table='ternary_table',
            priority=priority, matches=[dict(name='h.key.x',value='01',mask='ff',kind='ternary')])
    for label in ('normal','zero','max','padded','reverse','missing-param','no-params','extra-param','missing-field','unknown-field'):
        matches=[dict(name='h.key.x',value='01',kind='exact'),dict(name='h.key.y',value='02',kind='exact')]
        params=[dict(name='port',value='07'),dict(name='tag',value='09')]
        category='invalid' if label in ('missing-param','no-params','extra-param','missing-field','unknown-field') else 'normalizable' if label=='padded' else 'valid'
        if label=='zero':params=[dict(name='port',value='00'),dict(name='tag',value='00')]
        if label=='max':params=[dict(name='port',value='01ff'),dict(name='tag',value='ff')]
        if label=='padded':params=[dict(name='port',value='000007'),dict(name='tag',value='0009')]
        if label=='reverse':matches.reverse();params.reverse()
        if label=='missing-param':params.pop()
        if label=='no-params':params=[]
        if label=='extra-param':params.append(dict(name='typo',value='01'))
        if label=='missing-field':matches.pop()
        if label=='unknown-field':matches.append(dict(name='typo',value='01',kind='exact'))
        add('sparse-'+label,'sparse_ids',category,table='sparse',matches=matches,action='MyIngress.sparse_action',params=params)
    assert len(cases) == len({c['id'] for c in cases})
    (folder/'cases.json').write_text(json.dumps(cases,indent=2)+'\n')
    print(json.dumps(dict(cases=len(cases),tables=len(tables),sparse_field_ids=[13,5],sparse_param_ids=[19,7])))


if __name__=='__main__':build()
