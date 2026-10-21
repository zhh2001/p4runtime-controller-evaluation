"""Check constructor outputs against the fixture's explicit numerical inputs."""

import json
from pathlib import Path

from finsy.proto import p4i, p4r
from google.protobuf import json_format, text_format


ROOT = Path(__file__).resolve().parent


def verify(path):
    corpus = json.loads((ROOT / "fixtures/inputs.json").read_text())
    info = text_format.Parse((ROOT / "fixtures/construct.p4info.txt").read_text(), p4i.P4Info())
    tables = {t.preamble.id: t for t in info.tables}
    actions = {a.preamble.id: a for a in info.actions}
    records = json.loads(path.read_text())
    seen = set()
    for record in records:
        identity = record["profile"], record["workload"], record["mode"], record["index"]
        assert identity not in seen
        seen.add(identity)
        source = corpus[record["workload"]][record["index"]]
        entry = json_format.ParseDict(record["wire"], p4r.TableEntry())
        assert entry.table_id == source["table_id"] and entry.priority == source["priority"]
        assert entry.metadata.hex() == source["metadata"]
        assert not entry.is_default_action and entry.idle_timeout_ns == 0
        assert entry.action.WhichOneof("type") == "action"
        assert entry.action.action.action_id == source["action_id"]
        table, action = tables[entry.table_id], actions[entry.action.action.action_id]
        assert table.preamble.name == source["table"] and action.preamble.name == source["action"]
        actual = {m.field_id: m for m in entry.match}
        assert len(actual) == len(entry.match) == len(source["matches"])
        fields = {m.id: m for m in table.match_fields}
        for match in source["matches"]:
            field = actual[match["id"]]
            declaration = fields[match["id"]]
            assert declaration.name == match["field"]
            kind = match["kind"]
            assert field.WhichOneof("field_match_type") == kind
            body = getattr(field, kind)
            value = int(match["value"], 16)
            assert value < 1 << declaration.bitwidth
            if kind == "range":
                high = int(match["high"], 16)
                assert value <= high < 1 << declaration.bitwidth
                assert int.from_bytes(body.low, "big") == value and int.from_bytes(body.high, "big") == high
                assert 0 < len(body.low) <= (declaration.bitwidth + 7) // 8
                assert 0 < len(body.high) <= (declaration.bitwidth + 7) // 8
            else:
                assert int.from_bytes(body.value, "big") == value
                assert 0 < len(body.value) <= (declaration.bitwidth + 7) // 8
            if kind == "lpm":
                assert body.prefix_len == match["prefix"]
                assert value & ((1 << (declaration.bitwidth - body.prefix_len)) - 1) == 0
            elif kind == "ternary":
                mask = int(match["mask"], 16)
                assert mask and value & ~mask == 0 and int.from_bytes(body.mask, "big") == mask
                assert 0 < len(body.mask) <= (declaration.bitwidth + 7) // 8
        actual_params = {p.param_id: int.from_bytes(p.value, "big") for p in entry.action.action.params}
        assert len(actual_params) == len(entry.action.action.params) == len(source["params"])
        assert actual_params == {p["id"]: int(p["value"], 16) for p in source["params"]}
        parameters = {p.id: p for p in action.params}
        for param in entry.action.action.params:
            width = parameters[param.param_id].bitwidth
            assert actual_params[param.param_id] < 1 << width
            assert 0 < len(param.value) <= (width + 7) // 8
    return len(records)
