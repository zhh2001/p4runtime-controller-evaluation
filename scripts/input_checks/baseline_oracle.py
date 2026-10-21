"""Finite-corpus baseline wire checks."""

from finsy.proto import p4i


def number(data, width):
    if not data or int.from_bytes(data, "big").bit_length() > width:
        raise ValueError("value exceeds declared width or is empty")
    return int.from_bytes(data, "big")


def validate_entry(info, entry):
    """Check the subset exercised by this corpus without SDK encoder calls."""
    tables = {t.preamble.id: t for t in info.tables}
    actions = {a.preamble.id: a for a in info.actions}
    if entry.table_id not in tables:
        raise ValueError("unknown table ID")
    table = tables[entry.table_id]
    fields = {f.id: f for f in table.match_fields}
    seen = set()
    needs_priority = any(f.match_type in (p4i.MatchField.TERNARY, p4i.MatchField.RANGE, p4i.MatchField.OPTIONAL) for f in fields.values())
    if (needs_priority and entry.priority <= 0) or (not needs_priority and entry.priority != 0):
        raise ValueError("invalid priority for table")
    if entry.is_default_action:
        raise ValueError("default entries are outside this pilot")
    for match in entry.match:
        if match.field_id not in fields or match.field_id in seen:
            raise ValueError("unknown or duplicate match field")
        seen.add(match.field_id)
        field = fields[match.field_id]
        kind = match.WhichOneof("field_match_type")
        expected = p4i.MatchField.MatchType.Name(field.match_type).lower()
        if kind != expected:
            raise ValueError("match type differs from declaration")
        part = getattr(match, kind)
        if kind in ("exact", "optional"):
            number(part.value, field.bitwidth)
        elif kind == "lpm":
            value = number(part.value, field.bitwidth)
            if not 0 < part.prefix_len <= field.bitwidth:
                raise ValueError("invalid LPM prefix")
            if value & ((1 << (field.bitwidth - part.prefix_len)) - 1):
                raise ValueError("LPM host bits are set")
        elif kind == "ternary":
            value = number(part.value, field.bitwidth)
            mask = number(part.mask, field.bitwidth)
            if not mask or value & mask != value:
                raise ValueError("invalid ternary value or wildcard")
        elif kind == "range":
            low, high = number(part.low, field.bitwidth), number(part.high, field.bitwidth)
            if low > high or (low == 0 and high == (1 << field.bitwidth) - 1):
                raise ValueError("invalid range or explicit full-range wildcard")
    if any(f.match_type == p4i.MatchField.EXACT and f.id not in seen for f in fields.values()):
        raise ValueError("missing EXACT field")
    if entry.action.WhichOneof("type") != "action":
        raise ValueError("missing direct action")
    action = entry.action.action
    refs = {a.id for a in table.action_refs}
    if action.action_id not in actions or action.action_id not in refs:
        raise ValueError("unknown or disallowed action")
    params = {p.id: p for p in actions[action.action_id].params}
    seen_params = set()
    for param in action.params:
        if param.param_id not in params or param.param_id in seen_params:
            raise ValueError("unknown or duplicate action parameter")
        seen_params.add(param.param_id)
        number(param.value, params[param.param_id].bitwidth)
    if seen_params != set(params):
        raise ValueError("missing action parameter")
