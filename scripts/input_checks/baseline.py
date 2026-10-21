"""Recheck the selected E1 pilot records and complete table readbacks."""

import base64
from collections import Counter, defaultdict
import copy
import csv
import json

from finsy.proto import p4r
from google.protobuf import json_format

from .baseline_semantics import integer, semantic_match
from .baseline_oracle import validate_entry


def normalize_read(entry, info):
    result = copy.deepcopy(entry)
    table = next(table for table in info.tables if table.preamble.id == entry["table_id"])
    definitions = {field.id: field for field in table.match_fields}
    matches, changes = [], []
    for match in result.get("match", []):
        field = definitions[match["field_id"]]
        wildcard = False
        if "range" in match:
            wildcard = integer(match["range"]["low"]) == 0 and integer(match["range"]["high"]) == (1 << field.bitwidth) - 1
        elif "ternary" in match:
            wildcard = integer(match["ternary"]["mask"]) == 0
        elif "lpm" in match:
            wildcard = match["lpm"].get("prefix_len", 0) == 0
        if wildcard:
            changes.append({"field_id": field.id, "representation": "explicit wildcard in Read"})
        else:
            matches.append(match)
    result["match"] = matches
    return result, changes


def event_identity(event, mode):
    if mode == "controlled":
        return event["adapter"], event["case_id"]
    marker = event["request"]["updates"][0]["entity"]["table_entry"]["metadata"]
    suite, case_id, token = base64.b64decode(marker, validate=True).decode().split("|", 2)
    assert suite == "E1"
    return token, case_id


def verify(info, cases, observations, events, mode):
    corpus = {case["id"]: case for case in cases}
    indexed = {event_identity(event, mode): event for event in events}
    assert len(indexed) == len(events)
    results, counts, paired = [], Counter(), defaultdict(list)
    wildcard_readbacks = 0
    for row in observations:
        case = corpus[row["case_id"]]
        token = f"{row['adapter']}@{row['repetition']}"
        event = indexed.get((token, case["id"]))
        assert row["suite"] == "E1" and row["category"] == case["category"]
        assert row["rpc_observed"] == bool(event)
        assert row["stage"] in ("local_rejected", "unsupported_case", "request_completed")
        changes = []
        if event:
            request = json_format.ParseDict(event["request"], p4r.WriteRequest())
            assert request.device_id == request.election_id.low == 1 and len(request.updates) == 1
            assert request.updates[0].type == p4r.Update.INSERT
            assert request.updates[0].entity.WhichOneof("entity") == "table_entry"
            wire = request.updates[0].entity.table_entry
            assert wire.metadata.decode() == f"E1|{case['id']}|{token}"
            assert row["wire"] == event["request"]["updates"][0]["entity"]["table_entry"]
            codes = event["codes"] if mode == "controlled" else ([0] if event["rpc_code"] == 0 else [detail["code"] for detail in event["details"]])
            successful = not any(codes)
            assert len(codes) == 1 and row["call_succeeded"] == successful
            assert row["rpc_code"] == (0 if successful else 2)
            if mode == "bmv2":
                assert event["rpc_code"] == row["rpc_code"] and row["write_events"] == [event]
            if successful:
                assert case["category"] != "invalid"
                if mode == "controlled":
                    validate_entry(info, wire)
                    assert row["semantics_match"] is True
                    assert semantic_match(case, event["request"], info)
                else:
                    normalized_wire, _ = normalize_read(event["request"]["updates"][0]["entity"]["table_entry"], info)
                    validate_entry(info, json_format.ParseDict(normalized_wire, p4r.TableEntry()))
                outcome = "accepted"
            else:
                outcome = "target_rejected"
                if mode == "controlled":
                    try:
                        validate_entry(info, wire)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Controlled endpoint rejected a valid wire entry")
        else:
            outcome = "not_expressible" if row["stage"] == "unsupported_case" else "local_rejected"
            assert not row.get("call_succeeded")
        if case["category"] == "canonical_valid":
            assert outcome == "accepted"
        if case["category"] == "invalid":
            assert outcome != "accepted"
        if mode == "controlled":
            legacy_name = {"target_rejected": "server_rejected", "not_expressible": "unsupported_case"}.get(outcome, outcome)
            assert row["outcome"] == legacy_name
        else:
            assert row["before"] == []
            if outcome == "accepted":
                assert len(row["after"]) == 1
                normalized, changes = normalize_read(row["after"][0], info)
                validate_entry(info, json_format.ParseDict(normalized, p4r.TableEntry()))
                assert semantic_match(case, {"updates": [{"entity": {"table_entry": normalized}}]}, info)
            else:
                assert row["after"] == []
            wildcard_readbacks += bool(changes)
        evaluation = dict(profile=row["adapter"], repetition=row["repetition"], case_id=case["id"],
                          category=case["category"], outcome=outcome, rpc_observed=bool(event),
                          rpc_code=row.get("rpc_code", ""), wildcard_readback=bool(changes))
        results.append(evaluation)
        counts[row["adapter"], case["category"], outcome] += 1
        paired[row["adapter"], case["id"]].append((outcome, row.get("rpc_code"), bool(event), bool(changes)))
    assert all(len(values) == 2 and values[0] == values[1] for values in paired.values())
    return results, counts, dict(observations=len(observations), rpc_calls=len(events),
                                  wildcard_readbacks=wildcard_readbacks, repeated_classifications_identical=True)


def check_recorded_counts(path, observations, mode):
    with path.open(newline="") as source:
        saved = list(csv.DictReader(source))
    if mode == "controlled":
        for row in saved:
            counts = Counter((r["category"], r["outcome"]) for r in observations
                             if r["adapter"] == row["adapter"] and r["repetition"] == 1)
            for column in row:
                if column == "adapter":
                    continue
                if column.startswith("canonical_valid_"):
                    category, outcome = "canonical_valid", column.removeprefix("canonical_valid_")
                else:
                    category, outcome = column.split("_", 1)
                outcome = "unsupported_case" if outcome == "not_expressible" else outcome
                assert int(row[column]) == counts[category, outcome], column
    else:
        counts = Counter((r["adapter"], r["suite"], r["category"], r["stage"], str(r.get("rpc_code")))
                         for r in observations if r["repetition"] == 1)
        actual = {tuple(row[k] for k in ("adapter", "suite", "category", "stage", "rpc_code")): int(row["count_per_round"])
                  for row in saved}
        assert actual == counts
