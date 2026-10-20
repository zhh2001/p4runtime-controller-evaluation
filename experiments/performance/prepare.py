import hashlib
import json
from pathlib import Path
import subprocess

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parent
fixtures = ROOT/"fixtures"
original = fixtures/"control.compiler.txtpb"
subprocess.run(["p4c-bm2-ss","--p4v","16","--p4runtime-files",original.name,
                "-o","control.json","control.p4"],cwd=fixtures,check=True)
info = text_format.Parse(original.read_text(),p4i.P4Info())
removed = []
for table in info.tables:
    if table.HasField("initial_default_action"):
        table.ClearField("initial_default_action")
        removed.append(table.preamble.name)
(fixtures/"control.p4info.txt").write_text(text_format.MessageToString(info))
assert len(info.tables)==1 and info.tables[0].size==32768
assert info.tables[0].match_fields[0].bitwidth==32
record = {"removed":"Table.initial_default_action","tables":removed,
          "reason":"shared text schema for the pinned older Go protobuf",
          "files_sha256":{path.name:hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in fixtures.iterdir() if path.name!="compatibility.json"}}
(fixtures/"compatibility.json").write_text(json.dumps(record,indent=2)+"\n")
