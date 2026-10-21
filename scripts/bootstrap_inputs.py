"""Install pinned clients and build the three input-validation experiments."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from bootstrap_rpc import ROOT, RUNTIME, command, digest, environment, install_source, inventory
from bootstrap_construction import install_python

INPUTS = ROOT / "experiments/inputs"
SETUP = RUNTIME / "input-setup.json"
GROUPS = ("baseline", "boundaries", "constraints")


def check_provenance():
    record = json.loads((ROOT / "config/input-runner-provenance.json").read_text())
    for name, item in record["files"].items():
        if digest(ROOT / name) != item["export_sha256"]:
            raise ValueError(f"Input runner source changed: {name}")
        if item["unchanged"] and item["source_sha256"] != item["export_sha256"]:
            raise ValueError(f"Input source correspondence differs: {name}")
    return record


def input_files():
    selected = set()
    for folder in (INPUTS, ROOT / "scripts/input_checks"):
        selected.update(path for path in folder.rglob("*") if path.is_file() and path.suffix in (".py", ".go", ".mod", ".sum", ".json")
                        and not any(part in ("__pycache__", "bin", "fixtures") for part in path.relative_to(folder).parts))
    manifest = json.loads((ROOT / "data/inputs/manifest.json").read_text())
    selected.update(ROOT / name for name in manifest["files"])
    selected.add(ROOT / "data/inputs/manifest.json")
    selected.update(ROOT / name for name in ("config/dependencies.json", "config/requirements-finsy.txt",
        "config/requirements-shell.txt", "config/input-runner-provenance.json", "config/input-source-provenance.json",
        "scripts/bootstrap_rpc.py", "scripts/bootstrap_construction.py", "scripts/bootstrap_inputs.py",
        "scripts/run_inputs.py", "scripts/analyze_inputs.py", "scripts/verify_inputs.py"))
    return sorted(selected)


def python_signature(name):
    package = "finsy" if name == "finsy" else "p4runtime_sh"
    code = (f"import {package} as package; import grpc,google.protobuf,json; "
            "from google.protobuf.internal import api_implementation; "
            "print(json.dumps(dict(package=package.__file__,grpc=grpc.__version__,"
            "protobuf=google.protobuf.__version__,backend=api_implementation.Type())))")
    result = json.loads(subprocess.check_output([str(RUNTIME / name / "bin/python"), "-c", code],
                                               env=environment(), text=True))
    source = "finsy" if name == "finsy" else "p4runtime-shell"
    if not Path(result["package"]).resolve().is_relative_to(RUNTIME / "sources" / source):
        raise ValueError(f"Unexpected imported source for {name}")
    return result


def check_setup():
    check_provenance()
    setup = json.loads(SETUP.read_text())
    for field in ("input_sha256", "generated_sha256"):
        for name, expected in setup[field].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f"Input experiment setup changed: {name}. Run bootstrap again.")
    for name, expected in setup["source_inventory"].items():
        if inventory(RUNTIME / "sources" / name) != expected:
            raise ValueError(f"Dependency source changed: {name}")
    for name, expected in setup["python_signatures"].items():
        if python_signature(name) != expected:
            raise ValueError(f"Python environment changed: {name}")
    return setup


def check_versions():
    setup = check_setup()
    sources = {item["name"]: item["commit"] for item in setup["dependencies"]["sources"]}
    hashes = {name: value for name, value in setup["source_inventory"]["p4runtime-go-controller"].items()
              if name.endswith(".go") or Path(name).name in ("go.mod", "go.sum")}
    if len(hashes) != 148:
        raise ValueError("Unexpected SDK source inventory")
    return dict(sdk_commit=sources.pop("p4runtime-go-controller"), sdk_files_sha256=hashes, others=sources)


def snapshot_run(output, group):
    setup = check_setup()
    hashes = dict(setup["input_sha256"])
    prefix = "experiments/inputs/" + group + "/"
    hashes.update({name: value for name, value in setup["generated_sha256"].items() if name.startswith(prefix)})
    for name, expected in hashes.items():
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
        if digest(target) != expected:
            raise ValueError(f"Snapshot source changed: {name}")
    original = {"baseline": "minimal", "boundaries": "boundaries", "constraints": "table-constraints"}[group]
    for path in sorted((INPUTS / group / "fixtures").iterdir()):
        if path.is_file():
            name = "experiments/" + original + "/fixtures/" + path.name
            target = output / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            hashes[name] = digest(path)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="Optional finsy/ and shell/ wheel directories")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so semantic checks remain enabled.")
    if sys.version_info[:2] != (3, 12) or platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("Use CPython 3.12 on Linux x86_64.")
    for name in ("go", "cc", "p4c-bm2-ss", "simple_switch_grpc"):
        if shutil.which(name) is None:
            parser.error(f"Install {name} and put it on PATH.")
    check_provenance()
    RUNTIME.mkdir(exist_ok=True)
    state = ROOT / "results/input-setup"
    state.mkdir(parents=True, exist_ok=True)
    log = state / "bootstrap.log"
    log.write_text("")
    env = environment()
    env.pop("PYTHONOPTIMIZE", None)
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    sources = {item["name"]: install_source(item) for item in lock["sources"]}
    packages = {name: install_python(name, args.wheelhouse, env, log) for name in ("finsy", "shell")}
    goroot = command(["go", "env", "GOROOT"], ROOT, env, log)
    env["PATH"] = str(Path(goroot) / "bin") + os.pathsep + env["PATH"]
    go_version = command(["go", "version"], ROOT, env, log)
    if lock["go_toolchain"] not in go_version:
        raise ValueError(go_version)
    command([RUNTIME / "finsy/bin/python", INPUTS / "compile.py"], ROOT, env, log)
    for group in GROUPS:
        for stack in ("sdk", "legacy"):
            folder = INPUTS / group / ("go-" + stack)
            (INPUTS / group / "bin").mkdir(exist_ok=True)
            command(["go", "vet", "-mod=readonly", "./..."], folder, env, log)
            for race in (False, True):
                argv = ["go", "build", "-mod=readonly"] + (["-race"] if race else [])
                argv += ["-o", INPUTS / group / "bin" / ("go-" + stack + ("-race" if race else "")), "."]
                command(argv, folder, env, log)
            command(["go", "version", "-m", INPUTS / group / "bin" / ("go-" + stack)], ROOT, env, log)
        command([RUNTIME / "finsy/bin/python", "-m", "unittest", "discover", "-s", INPUTS / group / "tests", "-v"], ROOT, env, log)
    generated = sorted(path for group in GROUPS for name in ("fixtures", "bin")
                       for path in (INPUTS / group / name).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    setup = dict(completed_local=datetime.now().astimezone().isoformat(), dependencies=lock, source_inventory=sources,
                 python_environments=packages, python_signatures={name: python_signature(name) for name in ("finsy", "shell")},
                 go=go_version, p4c=command(["p4c-bm2-ss", "--version"], ROOT, env, log),
                 bmv2=command(["simple_switch_grpc", "--version"], ROOT, env, log),
                 input_sha256={str(path.relative_to(ROOT)): digest(path) for path in input_files()},
                 generated_sha256={str(path.relative_to(ROOT)): digest(path) for path in generated},
                 go_vet_modules=6, normal_and_race_binaries=12, oracle_tests_passed=True)
    SETUP.write_text(json.dumps(setup, indent=2) + "\n")
    print(json.dumps(dict(completed=True, setup=str(SETUP), modules=6, binaries=12)))


if __name__ == "__main__":
    main()
