"""Compile the E4 table fixture and verify its archived declarations and cases."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
from retry_checks import model


def main():
    archive = ROOT / "data/retries/fixtures"
    actual = (json.dumps({mode: model.cases(mode) for mode in ("controlled", "bmv2")}, indent=2) + "\n").encode()
    if actual != (archive / "cases.json").read_bytes():
        raise ValueError("Generated retry cases differ from the archive")
    FIXTURES.mkdir(exist_ok=True)
    for name in ("keys.p4", "keys.p4info.txt"):
        shutil.copyfile(archive / name, FIXTURES / name)
    argv = ["p4c-bm2-ss", "--p4v", "16", "--p4runtime-files", "keys.compiler.txtpb", "-o", "keys.json", "keys.p4"]
    result = subprocess.run(argv, cwd=FIXTURES, capture_output=True, text=True)
    (FIXTURES / "compiler.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    actual = text_format.Parse((FIXTURES / "keys.compiler.txtpb").read_text(), p4i.P4Info())
    for table in actual.tables:
        table.ClearField("initial_default_action")
    expected = text_format.Parse((archive / "keys.p4info.txt").read_text(), p4i.P4Info())
    if actual != expected:
        raise ValueError("Compiled retry P4Info differs from the archive")
    print(json.dumps(dict(compiled_programs=1, archived_declarations_match=True, archived_cases_match=True)))


if __name__ == "__main__":
    main()
