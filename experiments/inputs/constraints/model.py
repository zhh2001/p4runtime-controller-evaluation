"""Protocol construction and semantic decoding without any tested encoder."""

from finsy.proto import p4r


def lookup(info, field, name):
    return next(x for x in getattr(info,field) if x.preamble.name==name)


def uint(value):
    return int.from_bytes(value,'big')


def raw_entry(info,c):
    table=lookup(info,'tables',c['table'])
    entry=p4r.TableEntry(table_id=table.preamble.id,is_default_action=c['default'],priority=c['priority'],idle_timeout_ns=c['ttl'])
    for item in c['matches']:
        definition=next((f for f in table.match_fields if f.name==item['name']),None)
        match=entry.match.add(field_id=definition.id if definition else 0xffffff)
        if item['kind']=='exact':match.exact.value=bytes.fromhex(item['value'])
        else:match.ternary.CopyFrom(p4r.FieldMatch.Ternary(value=bytes.fromhex(item['value']),mask=bytes.fromhex(item['mask'])))
    kind=c['action_kind']
    if kind=='direct':
        action=lookup(info,'actions',c['action'])
        entry.action.action.action_id=action.preamble.id
        for item in c['params']:
            definition=next((p for p in action.params if p.name==item['name']),None)
            entry.action.action.params.add(param_id=definition.id if definition else 0xffffff,value=bytes.fromhex(item['value']))
    elif kind=='member':entry.action.action_profile_member_id=c['reference']
    elif kind=='group':entry.action.action_profile_group_id=c['reference']
    return entry


def action_semantics(action):
    kind=action.WhichOneof('type')
    if kind=='action':
        return dict(kind='direct',id=action.action.action_id,params=sorted([[p.param_id,uint(p.value)] for p in action.action.params]))
    if kind in ('action_profile_member_id','action_profile_group_id'):
        return dict(kind='member' if kind.endswith('member_id') else 'group',reference=getattr(action,kind))
    if kind=='action_profile_action_set':
        return dict(kind='action_set',actions=[dict(weight=a.weight,action=action_semantics(p4r.TableAction(action=a.action))) for a in action.action_profile_action_set.action_profile_actions])
    return None


def semantics(entry,delete=False):
    result=dict(table=entry.table_id,default=entry.is_default_action,priority=entry.priority,
        matches=sorted([[f.field_id,f.WhichOneof('field_match_type'),uint(f.exact.value)] if f.HasField('exact') else
                        [f.field_id,'ternary',uint(f.ternary.value),uint(f.ternary.mask)] for f in entry.match]))
    if not delete:result.update(action=action_semantics(entry.action),ttl=entry.idle_timeout_ns)
    return result


def key(entry):
    value=semantics(entry,True)
    return (value['table'],value['default'],value['priority'],tuple(tuple(m) for m in value['matches']))


def default_entry(info,table):
    name='NoAction' if table.implementation_id or table.preamble.alias=='sparse' else 'MyIngress.common'
    action=lookup(info,'actions',name)
    entry=p4r.TableEntry(table_id=table.preamble.id,is_default_action=True,action=p4r.TableAction(action=p4r.Action(action_id=action.preamble.id)))
    for param in action.params:entry.action.action.params.add(param_id=param.id,value=b'\x00')
    return entry


def initial_entries(info):
    out=[]
    for table in info.tables:
        out.append(default_entry(info,table))
        matches=[dict(name=f.name,value='02' if f.name.endswith('.y') else '01',kind='ternary' if f.match_type==4 else 'exact',mask='ff') for f in table.match_fields]
        case=dict(table=table.preamble.name,default=False,priority=10 if any(f.match_type==4 for f in table.match_fields) else 0,ttl=0,
                  matches=matches,action_kind='member' if table.implementation_id else 'direct',reference=2,
                  action='MyIngress.sparse_action' if table.preamble.alias=='sparse' else 'MyIngress.common',
                  params=[dict(name='port',value='01')]+([dict(name='tag',value='02')] if table.preamble.alias=='sparse' else []))
        out.append(raw_entry(info,case))
    return out


def intent(info,c):
    entry=raw_entry(info,c)
    if c['category']=='policy':
        entry.ClearField('match');entry.priority=0
    if c['update']=='DELETE':return semantics(entry,True)
    if c['default'] and c['action_kind']=='none':
        entry.action.CopyFrom(default_entry(info,lookup(info,'tables',c['table'])).action)
    return semantics(entry)
