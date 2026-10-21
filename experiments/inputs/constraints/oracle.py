"""P4Runtime table input checks, independent of all tested implementations."""

from finsy.proto import p4i
from model import semantics, default_entry, key


class Invalid(Exception):
    def __init__(self,message,code=3):
        super().__init__(message);self.code=code


def validate(info,entry,operation,state_keys=None):
    value=_validate_fields(info,entry,operation)
    if state_keys is not None and not entry.is_default_action:
        identity=key(entry)
        if operation=='INSERT' and identity in state_keys:raise Invalid('already exists',6)
        if operation in ('MODIFY','DELETE') and identity not in state_keys:raise Invalid('entry not found',5)
    return value


def _validate_fields(info,entry,operation):
    table=next((t for t in info.tables if t.preamble.id==entry.table_id),None)
    if table is None:raise Invalid('unknown table')
    if entry.is_default_action:
        if operation!='MODIFY':raise Invalid('default entries require MODIFY')
        if entry.match or entry.priority:raise Invalid('default entries require empty match and zero priority')
        if table.const_default_action_id or table.implementation_id:raise Invalid('constant default',7)
    else:
        requires_priority=any(f.match_type in (p4i.MatchField.TERNARY,p4i.MatchField.RANGE,p4i.MatchField.OPTIONAL) for f in table.match_fields)
        if entry.priority==0 if requires_priority else entry.priority!=0:raise Invalid('priority does not match table')
        definitions={f.id:f for f in table.match_fields}
        seen=set()
        for match in entry.match:
            definition=definitions.get(match.field_id)
            if definition is None or match.field_id in seen:raise Invalid('unknown or duplicate field id')
            seen.add(match.field_id)
            expected='ternary' if definition.match_type==p4i.MatchField.TERNARY else 'exact'
            if match.WhichOneof('field_match_type')!=expected:raise Invalid('wrong match type')
            value=getattr(match,expected).value
            if not value or int.from_bytes(value,'big') >= 1<<definition.bitwidth:raise Invalid('match overflow')
            if expected=='ternary':
                mask=match.ternary.mask
                if not mask or not int.from_bytes(mask,'big') or int.from_bytes(mask,'big')>=1<<definition.bitwidth:raise Invalid('invalid ternary mask')
                if int.from_bytes(value,'big') & ~int.from_bytes(mask,'big'):raise Invalid('masked bits nonzero')
        if any(f.match_type==p4i.MatchField.EXACT and f.id not in seen for f in table.match_fields):raise Invalid('missing exact field')
        if table.is_const_table and (operation!='MODIFY' or entry.HasField('action')):raise Invalid('immutable constant table entry',7)
    if operation=='DELETE':return semantics(entry,True)
    if entry.idle_timeout_ns<0:raise Invalid('negative TTL')
    if entry.idle_timeout_ns and (entry.is_default_action or table.idle_timeout_behavior!=p4i.Table.NOTIFY_CONTROL):raise Invalid('unsupported TTL')
    kind=entry.action.WhichOneof('type')
    if kind is None:
        if not entry.is_default_action:raise Invalid('missing action')
        result=semantics(entry);result['action']=semantics(default_entry(info,table))['action'];return result
    if table.implementation_id:
        profile=next(p for p in info.action_profiles if p.preamble.id==table.implementation_id)
        if kind=='action_profile_member_id':reference=entry.action.action_profile_member_id
        elif kind=='action_profile_group_id' and profile.with_selector:reference=entry.action.action_profile_group_id
        else:raise Invalid('invalid indirect action alternative')
        if reference==0:raise Invalid('zero indirect reference')
        if reference!=1:raise Invalid('unknown indirect reference',5)
        return semantics(entry)
    if kind!='action':raise Invalid('direct table requires direct action')
    action=entry.action.action
    definition=next((a for a in info.actions if a.preamble.id==action.action_id),None)
    ref=next((r for r in table.action_refs if r.id==action.action_id),None)
    if definition is None or ref is None:raise Invalid('action not allowed in table')
    if ref.scope==p4i.ActionRef.TABLE_ONLY and entry.is_default_action or ref.scope==p4i.ActionRef.DEFAULT_ONLY and not entry.is_default_action:
        raise Invalid('action scope violation',7)
    params={p.id:p for p in definition.params};seen=set()
    for param in action.params:
        declaration=params.get(param.param_id)
        if declaration is None or param.param_id in seen:raise Invalid('unknown or duplicate parameter id')
        seen.add(param.param_id)
        if not param.value or int.from_bytes(param.value,'big')>=1<<declaration.bitwidth:raise Invalid('parameter overflow')
    if seen!=set(params):raise Invalid('missing action parameter')
    return semantics(entry)
