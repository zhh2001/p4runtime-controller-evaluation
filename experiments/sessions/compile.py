"""Compile the session fixture and compare its declarations with the archive."""

from pathlib import Path
import json
import shutil
import subprocess

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main():
    FIXTURES.mkdir(exist_ok=True)
    archived = ROOT / "data/sessions/fixtures"
    for path in archived.iterdir():
        shutil.copyfile(path, FIXTURES / path.name)
    command = ["p4c-bm2-ss", "--p4v", "16", "--p4runtime-files", "keys.compiler.txtpb", "-o", "keys.json", "keys.p4"]
    result = subprocess.run(command, cwd=FIXTURES, capture_output=True, text=True)
    (FIXTURES / "compiler.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    actual = text_format.Parse((FIXTURES / "keys.compiler.txtpb").read_text(), p4i.P4Info())
    for table in actual.tables:
        table.ClearField("initial_default_action")
    expected = text_format.Parse((archived / "keys.p4info.txt").read_text(), p4i.P4Info())
    if actual != expected:
        raise ValueError("Compiled session P4Info differs from the archive")
    (FIXTURES / "keys.p4info.txt").write_bytes((archived / "keys.p4info.txt").read_bytes())
    print(json.dumps(dict(compiled_programs=1, archived_declarations_match=True)))


if __name__ == "__main__":
    main()
