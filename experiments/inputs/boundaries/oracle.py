"""Independent numerical/type checks for the declared boundary fixture."""


class Invalid(ValueError):
    def __init__(self, message, code=3):
        super().__init__(message)
        self.code = code


def require(condition, message, code=3):
    if not condition:
        raise Invalid(message, code)


def number(data, width, signed=False):
    require(bool(data), "empty integer encoding", 11)
    value = int.from_bytes(data, "big", signed=signed)
    low, high = (-(1 << (width-1)), (1 << (width-1))-1) if signed else (0, (1 << width)-1)
    require(low <= value <= high, f"integer does not fit declared {'int' if signed else 'bit'}<{width}>", 11)
    return value


def data_value(spec, value, types):
    kind = spec.WhichOneof("type_spec")
    actual = value.WhichOneof("data")
    if kind == "new_type":
        declaration = types.new_types[spec.new_type.name]
        if declaration.WhichOneof("representation") == "original_type":
            return data_value(declaration.original_type, value, types)
        require(actual == "bitstring", "translated data must use bitstring")
        translation = declaration.translated_type
        if translation.WhichOneof("sdn_type") == "sdn_string":
            return dict(text_bytes=value.bitstring.hex())
        return number(value.bitstring, translation.sdn_bitwidth)
    if kind == "bitstring":
        bits = spec.bitstring
        subtype = bits.WhichOneof("type_spec")
        if subtype == "varbit":
            require(actual == "varbit", "expected varbit")
            width = value.varbit.bitwidth
            require(0 <= width <= bits.varbit.max_bitwidth, "varbit width outside declaration", 11)
            # Compare the explicit dynamic width and numerical content. This
            # corpus does not classify alternative varbit byte lengths.
            numeric = int.from_bytes(value.varbit.bitstring, "big")
            require(numeric < (1 << width), "varbit content exceeds dynamic width", 11)
            return dict(value=numeric, bitwidth=width)
        require(actual == "bitstring", "expected integer bitstring")
        return number(value.bitstring, getattr(bits, subtype).bitwidth, subtype == "int")
    if kind == "bool":
        require(actual == "bool", "expected bool")
        return value.bool
    if kind in ("tuple", "struct"):
        require(actual == kind, "compound data tag differs from declaration")
        declarations = list(spec.tuple.members) if kind == "tuple" else [m.type_spec for m in types.structs[spec.struct.name].members]
        actual_members = getattr(value, kind).members
        require(len(actual_members) == len(declarations), "compound member count differs")
        return [data_value(t, v, types) for t, v in zip(declarations, actual_members)]
    raise Invalid(f"type outside this corpus: {kind}")


def validate(info, entity, operation):
    kind = entity.WhichOneof("entity")
    if kind == "table_entry":
        entry = entity.table_entry
        tables = {t.preamble.id: t for t in info.tables}
        require(entry.table_id in tables, "unknown table")
        table = tables[entry.table_id]
        require(not entry.is_default_action and entry.priority == 0, "not an ordinary EXACT entry")
        require(len(entry.match) == 1, "expected one match")
        field = entry.match[0]
        require(field.field_id == table.match_fields[0].id and field.WhichOneof("field_match_type") == "exact", "wrong match field")
        key = number(field.exact.value, table.match_fields[0].bitwidth)
        require(entry.action.WhichOneof("type") == "action", "expected direct action")
        action = entry.action.action
        declarations = {a.preamble.id: a for a in info.actions}
        require(action.action_id in {r.id for r in table.action_refs}, "action outside table")
        declaration = declarations[action.action_id]
        require(len(action.params) == len(declaration.params) == 1, "wrong action parameters")
        require(action.params[0].param_id == declaration.params[0].id, "wrong parameter ID")
        value = number(action.params[0].value, declaration.params[0].bitwidth)
        return dict(kind=kind, id=entry.table_id, key=key, value=value)
    require(kind in ("counter_entry", "meter_entry", "register_entry"), "entity outside corpus")
    entry = getattr(entity, kind)
    base = kind.removesuffix("_entry")
    identifier = getattr(entry, base + "_id")
    declarations = {v.preamble.id: v for v in getattr(info, {"counter": "counters", "meter": "meters", "register": "registers"}[base])}
    require(identifier in declarations, "unknown resource")
    declaration = declarations[identifier]
    if entry.HasField("index"):
        index = entry.index.index
        require(index >= 0, "negative index")
        if declaration.HasField("index_type_name"):
            require(index in (10000, 10001), "index absent from controlled translation map", 11)
        else:
            require(index < declaration.size, "index outside array", 11)
    else:
        index = "all"
        require(operation == "read", "bulk writes outside this corpus")
    result = dict(kind=kind, id=identifier, index=index)
    if operation == "write":
        if kind == "register_entry":
            require(entry.HasField("data"), "register data missing")
            result["data"] = data_value(declaration.type_spec, entry.data, info.type_info)
        elif kind == "counter_entry":
            require(entry.HasField("data"), "counter data missing")
            result["data"] = dict(packets=entry.data.packet_count, bytes=entry.data.byte_count)
        else:
            require(entry.HasField("config"), "meter config missing")
            result["data"] = dict(cir=entry.config.cir, pir=entry.config.pir, cburst=entry.config.cburst, pburst=entry.config.pburst)
    return result
