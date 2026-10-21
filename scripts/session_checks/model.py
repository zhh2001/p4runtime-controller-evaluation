"""Fixture-based entry helpers used by the original evidence checks."""

import hashlib
import json
from pathlib import Path

from finsy.proto import p4i, p4r
from google.protobuf import json_format, text_format

ROOT = Path(__file__).resolve().parents[2]
INFO = text_format.Parse((ROOT / "data/sessions/fixtures/keys.p4info.txt").read_text(), p4i.P4Info())


def message(value):
    return json_format.MessageToDict(value,preserving_proto_field_name=True)


def entry(key,marker=b"seed"):
    table=next(table for table in INFO.tables if table.preamble.name=="MyIngress.t_exact")
    action=next(action for action in INFO.actions if action.preamble.name=="MyIngress.forward")
    return p4r.TableEntry(table_id=table.preamble.id,match=[p4r.FieldMatch(field_id=table.match_fields[0].id,exact=p4r.FieldMatch.Exact(value=key.to_bytes(max(1,(key.bit_length()+7)//8),"big")))],action=p4r.TableAction(action=p4r.Action(action_id=action.preamble.id,params=[p4r.Action.Param(param_id=action.params[0].id,value=b"\x01")])),metadata=marker)


def expected_entry(port,marker):
    value=entry(1,marker)
    value.action.action.params[0].value=bytes([port])
    return message(value)


def verify_snapshot(output):
    record = json.loads((output / "manifest.json").read_text())
    for name, digest in record["files_sha256"].items():
        assert hashlib.sha256((output / "source" / name).read_bytes()).hexdigest() == digest, name
    return record
