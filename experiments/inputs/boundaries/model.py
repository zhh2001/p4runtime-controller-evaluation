"""Protocol-only construction and semantic expectations, independent of SDKs."""

from finsy.proto import p4d, p4r
from google.protobuf import json_format


def declaration(info, kind, name):
    field = {"table": "tables", "action": "actions", "counter": "counters", "meter": "meters", "register": "registers"}[kind]
    return next(item for item in getattr(info, field) if item.preamble.name == name)


def raw_entity(info, case):
    kind = case["kind"]
    if kind in ("match", "param"):
        table = declaration(info, "table", case["table"])
        action = declaration(info, "action", case["action"])
        return p4r.Entity(table_entry=p4r.TableEntry(table_id=table.preamble.id,
            match=[p4r.FieldMatch(field_id=table.match_fields[0].id, exact=p4r.FieldMatch.Exact(value=bytes.fromhex(case["key"])))],
            action=p4r.TableAction(action=p4r.Action(action_id=action.preamble.id,
                params=[p4r.Action.Param(param_id=action.params[0].id,value=bytes.fromhex(case["action_value"]))]))))
    definition = declaration(info, kind, case["resource"])
    entity = p4r.Entity()
    entry = getattr(entity, kind+"_entry")
    setattr(entry, kind+"_id", definition.preamble.id)
    if not (case["op"] == "read" and case["index"] == -1): entry.index.index = case["index"]
    if case["op"] == "write":
        if kind == "register": entry.data.CopyFrom(json_format.ParseDict(case["data"], p4d.P4Data()))
        elif kind == "counter": entry.data.CopyFrom(p4r.CounterData(packet_count=11, byte_count=704))
        else: entry.config.CopyFrom(p4r.MeterConfig(cir=10, pir=20, cburst=2, pburst=4))
    return entity


def intent(info, case):
    if case["kind"] in ("match", "param"):
        return dict(kind="table_entry", id=declaration(info,"table",case["table"]).preamble.id, **case["expected"])
    kind = case["kind"]
    result = dict(kind=kind+"_entry", id=declaration(info,kind,case["resource"]).preamble.id,
                  index="all" if case["op"] == "read" and case["index"] == -1 else case["index"])
    if case["op"] == "write":
        result["data"] = case["expected"] if kind == "register" else dict(packets=11,bytes=704) if kind == "counter" else dict(cir=10,pir=20,cburst=2,pburst=4)
    return result
