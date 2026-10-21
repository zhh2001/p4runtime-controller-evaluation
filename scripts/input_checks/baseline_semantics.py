"""Numerical comparison for the baseline input corpus."""

import base64


def integer(value):
    return int.from_bytes(base64.b64decode(value), "big")


def semantic_match(case, wire, info):
    table = next(t for t in info.tables if t.preamble.name == case["table"])
    actual = wire["updates"][0]["entity"]["table_entry"]
    if actual.get("table_id", 0) != table.preamble.id:
        return False
    fields = {f.id: f for f in table.match_fields}
    expected = {}
    for match in case["matches"]:
        field = next(f for f in fields.values() if f.name == match["field"])
        value = int(match["value"], 16)
        kind = match["kind"]
        item = {"kind": kind}
        if kind == "lpm":
            if match["prefix"] == 0:
                continue
            item.update(value=value & ~((1 << (field.bitwidth-match["prefix"]))-1), prefix=match["prefix"])
        elif kind == "ternary":
            mask = int(match["mask"],16)
            if mask == 0:
                continue
            item.update(value=value & mask, mask=mask)
        elif kind == "range":
            high = int(match["high"],16)
            if value == 0 and high == (1 << field.bitwidth)-1:
                continue
            item.update(low=value, high=high)
        else:
            item["value"] = value
        expected[field.id] = item
    observed = {}
    for match in actual.get("match", []):
        kind = next(key for key in ("exact","lpm","ternary","range","optional") if key in match)
        value = match[kind]
        item = {"kind":kind}
        if kind == "range":
            item.update(low=integer(value["low"]), high=integer(value["high"]))
        else:
            item["value"] = integer(value["value"])
            if kind == "lpm": item["prefix"] = value.get("prefix_len",0)
            if kind == "ternary": item["mask"] = integer(value["mask"])
        observed[match["field_id"]] = item
    action = next(a for a in info.actions if a.preamble.name == case["action"])
    actual_action = actual.get("action",{}).get("action",{})
    expected_params = {p.id:int(case["params"][p.name],16) for p in action.params}
    actual_params = {p["param_id"]:integer(p["value"]) for p in actual_action.get("params",[])}
    return expected == observed and actual_action.get("action_id") == action.preamble.id and expected_params == actual_params and actual.get("priority",0) == case["priority"]
