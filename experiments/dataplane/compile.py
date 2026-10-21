"""Compile the pinned L2 fixture and compare both P4Info forms with the archive."""

import json
from pathlib import Path
import subprocess

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def main():
    archive = ROOT / 'data/dataplane/fixtures'
    source = ROOT / '.runtime/sources/p4runtime-go-controller/examples/testdata/l2.p4'
    if source.read_bytes() != (archive / 'l2.p4').read_bytes():
        raise ValueError('Released P4 source differs from the archived fixture')
    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / 'l2.p4').write_bytes(source.read_bytes())
    argv = ['p4c-bm2-ss', '--arch', 'v1model', '--p4runtime-files', 'l2.compiler.txtpb', '-o', 'l2.json', 'l2.p4']
    result = subprocess.run(argv, cwd=FIXTURES, capture_output=True, text=True)
    (FIXTURES / 'compiler.log').write_text(result.stdout + result.stderr)
    result.check_returncode()
    actual = text_format.Parse((FIXTURES / 'l2.compiler.txtpb').read_text(), p4i.P4Info())
    expected = text_format.Parse((archive / 'l2.compiler.txtpb').read_text(), p4i.P4Info())
    if actual != expected:
        raise ValueError('Compiler P4Info differs from the archive')
    for table in actual.tables:
        table.ClearField('initial_default_action')
    expected = text_format.Parse((archive / 'l2.p4info.txt').read_text(), p4i.P4Info())
    if actual != expected:
        raise ValueError('Compatible P4Info differs from the archive')
    (FIXTURES / 'l2.p4info.txt').write_bytes((archive / 'l2.p4info.txt').read_bytes())
    (FIXTURES / 'empty.yaml').write_text('{}\n')
    print(json.dumps(dict(compiled_programs=1, archived_declarations_match=True)))


if __name__ == '__main__':
    main()
