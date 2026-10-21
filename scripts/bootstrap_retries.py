"""Install pinned clients and build the batch-write retry experiments."""

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

RETRIES = ROOT / "experiments/retries"
SETUP = RUNTIME / "retry-setup.json"
TARGETS = ("controlled", "bmv2")
SMOKE_CASES = ("transient-rebased", "short-details", "transport-partial", "mixed-conflicts", "all-duplicates")


def selection(target, mode):
    plan = json.loads((ROOT / "data/retries/plan.json").read_text())
    cases = json.loads((ROOT / "data/retries/fixtures/cases.json").read_text())[target]
    if mode == "smoke":
        cases = [case for case in cases if case["id"] in SMOKE_CASES]
    profiles = ["sdk", "legacy"] if mode == "race" else plan["profiles"]
    repetitions = 2 if mode == "full" else 1
    return plan, cases, profiles, repetitions


def check_provenance():
    records = []
    archived = json.loads((ROOT / "data/retries/controlled/run.json").read_text())["source_sha256"]
    for name in ("retry-source-provenance.json", "retry-runner-provenance.json"):
        record = json.loads((ROOT / "config" / name).read_text())
        for filename, item in record["files"].items():
            if digest(ROOT / filename) != item["export_sha256"]:
                raise ValueError(f"Retry source changed: {filename}")
            original = item["source_file"].split("/source/", 1)[1]
            if archived[original] != item["source_sha256"]:
                raise ValueError(f"Retry execution-source binding differs: {filename}")
            if item["unchanged"] and item["source_sha256"] != item["export_sha256"]:
                raise ValueError(f"Retry source correspondence differs: {filename}")
        records.append(record)
    return records


def input_files():
    selected = set()
    for directory in (RETRIES, ROOT / "scripts/retry_checks", ROOT / "tests/retries"):
        selected.update(path for path in directory.rglob("*") if path.is_file() and
                        path.suffix in (".py", ".go", ".mod", ".sum", ".json") and
                        not any(part in ("__pycache__", "bin", "fixtures") for part in path.relative_to(directory).parts))
    selected.update(path for path in (ROOT / "data/retries/fixtures").iterdir() if path.is_file())
    selected.update(ROOT / name for name in ("data/retries/plan.json", "data/retries/controlled/run.json",
        "data/retries/bmv2/run.json", "config/dependencies.json", "config/requirements-finsy.txt",
        "config/requirements-shell.txt", "config/retry-source-provenance.json", "config/retry-runner-provenance.json",
        "scripts/bootstrap_rpc.py", "scripts/bootstrap_construction.py", "scripts/bootstrap_retries.py",
        "scripts/bootstrap_sessions.py", "scripts/run_sessions.py", "scripts/run_retries.py",
        "scripts/analyze_retries.py", "scripts/verify_retries.py"))
    return sorted(selected)


def python_signatures():
    result = {}
    for name, package, source, expected in (("finsy", "finsy", "finsy", "upb"),
                                             ("shell", "p4runtime_sh", "p4runtime-shell", "python")):
        code = (f"import {package} as package; import grpc,google.protobuf,json,platform;"
                "from google.protobuf.internal import api_implementation;"
                "print(json.dumps(dict(package=package.__file__,python=platform.python_version(),"
                "grpc=grpc.__version__,protobuf=google.protobuf.__version__,backend=api_implementation.Type())))")
        value = json.loads(subprocess.check_output([str(RUNTIME / name / "bin/python"), "-c", code],
                                                  env=environment(), text=True))
        if not Path(value["package"]).resolve().is_relative_to(RUNTIME / "sources" / source) or value["backend"] != expected:
            raise ValueError(f"Unexpected pinned Python environment: {name}")
        result[name] = value
    return result


def check_setup():
    check_provenance()
    record = json.loads(SETUP.read_text())
    for field in ("input_sha256", "generated_sha256"):
        for name, expected in record[field].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f"Retry setup changed: {name}. Run bootstrap again.")
    for name, expected in record["source_inventory"].items():
        if inventory(RUNTIME / "sources" / name) != expected:
            raise ValueError(f"Dependency source changed: {name}")
    if python_signatures() != record["python_signatures"]:
        raise ValueError("Retry Python environment changed")
    return record


def snapshot_run(output):
    record = check_setup()
    hashes = {**record["input_sha256"], **record["generated_sha256"]}
    for name, expected in hashes.items():
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
        if digest(target) != expected:
            raise ValueError(f"Snapshot input changed: {name}")
    # The unchanged wire decoder reads the frozen corpus at its execution-era path.
    name = "experiments/batch-retry/cases.json"
    target = output / "source" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((ROOT / "data/retries/fixtures/cases.json").read_bytes())
    hashes[name] = digest(target)
    return record, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="Optional finsy/ and shell/ wheel directories")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so evidence checks remain enabled.")
    if sys.version_info[:2] != (3, 12) or platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("Use CPython 3.12 on Linux x86_64.")
    for name in ("go", "cc", "p4c-bm2-ss", "simple_switch_grpc"):
        if shutil.which(name) is None:
            parser.error(f"Install {name} and put it on PATH.")
    check_provenance()
    RUNTIME.mkdir(exist_ok=True)
    output = ROOT / "results/retry-setup"
    output.mkdir(parents=True, exist_ok=True)
    log = output / "bootstrap.log"
    log.write_text("")
    env = environment()
    env.pop("PYTHONOPTIMIZE", None)
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    sources = {item["name"]: install_source(item) for item in lock["sources"]}
    original = json.loads((ROOT / "data/retries/controlled/run.json").read_text())["versions"]
    sdk = {name: value for name, value in sources["p4runtime-go-controller"].items()
           if name.endswith(".go") or Path(name).name in ("go.mod", "go.sum")}
    versions = dict(sdk_commit=lock["sources"][0]["commit"], sdk_files_sha256=sdk,
                    others={item["name"]: item["commit"] for item in lock["sources"][1:]})
    if versions != original or versions != json.loads((ROOT / "data/retries/bmv2/run.json").read_text())["versions"]:
        raise ValueError("Pinned clients differ from the archived retry environment")
    packages = {name: install_python(name, args.wheelhouse, env, log) for name in ("finsy", "shell")}
    goroot = command(["go", "env", "GOROOT"], ROOT, env, log)
    env["PATH"] = str(Path(goroot) / "bin") + os.pathsep + env["PATH"]
    go_version = command(["go", "version"], ROOT, env, log)
    if lock["go_toolchain"] not in go_version:
        raise ValueError(go_version)
    command([RUNTIME / "finsy/bin/python", RETRIES / "compile.py"], ROOT, env, log)
    (RETRIES / "bin").mkdir(exist_ok=True)
    for stack in ("sdk", "legacy"):
        folder = RETRIES / ("go-" + stack)
        command(["go", "vet", "-mod=readonly", "./..."], folder, env, log)
        for race in (False, True):
            argv = ["go", "build", "-mod=readonly"] + (["-race"] if race else [])
            binary = RETRIES / "bin" / ("go-" + stack + ("-race" if race else ""))
            command(argv + ["-o", binary, "."], folder, env, log)
            command(["go", "version", "-m", binary], ROOT, env, log)
    command([RUNTIME / "finsy/bin/python", "-m", "unittest", "discover", "-s", ROOT / "tests/retries", "-v"], ROOT, env, log)
    generated = sorted(path for name in ("fixtures", "bin") for path in (RETRIES / name).rglob("*")
                       if path.is_file() and "__pycache__" not in path.parts)
    record = dict(completed_local=datetime.now().astimezone().isoformat(), dependencies=lock, versions=versions,
                  source_inventory=sources, python_environments=packages, python_signatures=python_signatures(),
                  go=go_version, p4c=command(["p4c-bm2-ss", "--version"], ROOT, env, log),
                  bmv2=command(["simple_switch_grpc", "--version"], ROOT, env, log),
                  input_sha256={str(path.relative_to(ROOT)): digest(path) for path in input_files()},
                  generated_sha256={str(path.relative_to(ROOT)): digest(path) for path in generated},
                  go_vet_modules=2, normal_and_race_binaries=4)
    SETUP.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(dict(completed=True, modules=2, binaries=4)))


if __name__ == "__main__":
    main()
