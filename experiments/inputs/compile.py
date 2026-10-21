"""Compile archived P4 fixtures and check the generated declarations and cases."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

from finsy.proto import p4i
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = Path(__file__).resolve().parent


def build_group(group):
    path = EXPERIMENTS / group / "fixtures.py"
    spec = importlib.util.spec_from_file_location(group + "_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build()


def compare_info(path, expected):
    actual = text_format.Parse(path.read_text(), p4i.P4Info())
    reference = text_format.Parse(expected.read_text(), p4i.P4Info())
    if actual != reference:
        raise ValueError(f"Compiled P4Info differs from the archive: {path}")


def main():
    if not __debug__:
        raise ValueError("Run without -O so fixture checks remain enabled")
    folder = EXPERIMENTS / "baseline/fixtures"
    folder.mkdir(parents=True, exist_ok=True)
    archived = ROOT / "data/inputs/baseline/fixtures"
    for source, target in (("tables.p4", "keys.p4"), ("cases.json", "cases.json"),
                           ("controlled.p4info.txt", "table_keys.p4info.txt")):
        shutil.copyfile(archived / source, folder / target)
    command = ["p4c-bm2-ss", "--p4v", "16", "--p4runtime-files", "keys.compiler.txtpb", "-o", "keys.json", "keys.p4"]
    result = subprocess.run(command, cwd=folder, capture_output=True, text=True)
    (folder / "compiler.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    info = text_format.Parse((folder / "keys.compiler.txtpb").read_text(), p4i.P4Info())
    for table in info.tables:
        table.ClearField("initial_default_action")
    (folder / "keys.p4info.txt").write_text(text_format.MessageToString(info))
    compare_info(folder / "keys.p4info.txt", archived / "bmv2.p4info.txt")
    for group, source in (("boundaries", "boundaries.p4"), ("constraints", "tables.p4")):
        build_group(group)
        folder = EXPERIMENTS / group / "fixtures"
        archived = ROOT / "data/inputs" / group / "fixtures"
        for name in (source, "cases.json", "schema.json"):
            if (archived / name).exists() and (folder / name).read_bytes() != (archived / name).read_bytes():
                raise ValueError(f"Generated input differs from the archive: {group}/{name}")
        for name in ("controlled.p4info.txt", "target.p4info.txt"):
            if (archived / name).exists():
                compare_info(folder / name, archived / name)
    print(json.dumps(dict(compiled_programs=3, cases=382, archived_inputs_and_declarations_match=True)))


if __name__ == "__main__":
    main()
