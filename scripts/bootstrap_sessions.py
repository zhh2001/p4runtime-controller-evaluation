"""Install pinned clients and build the session and stream experiments."""

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

SESSIONS = ROOT / "experiments/sessions"
SETUP = RUNTIME / "session-setup.json"


def check_provenance():
    records = []
    for name in ("session-source-provenance.json", "session-runner-provenance.json"):
        record = json.loads((ROOT / "config" / name).read_text())
        for filename, item in record["files"].items():
            if digest(ROOT / filename) != item["export_sha256"]:
                raise ValueError(f"Session source changed: {filename}")
            if item.get("unchanged") and item["source_sha256"] != item["export_sha256"]:
                raise ValueError(f"Session source correspondence differs: {filename}")
        records.append(record)
    if (SESSIONS / "formal-plan.json").read_bytes() != (ROOT / "data/sessions/plan.json").read_bytes():
        raise ValueError("Session plan differs from the archive")
    return records


def input_files():
    selected = set()
    for directory in (SESSIONS, ROOT / "scripts/session_checks"):
        selected.update(path for path in directory.rglob("*") if path.is_file() and
                        path.suffix in (".py", ".go", ".mod", ".sum", ".json") and
                        not any(part in ("__pycache__", "bin", "fixtures") for part in path.relative_to(directory).parts))
    selected.update(path for path in (ROOT / "data/sessions/fixtures").iterdir() if path.is_file())
    selected.update(ROOT / name for name in ("data/sessions/plan.json", "data/sessions/environment.json",
        "config/dependencies.json", "config/requirements-finsy.txt", "config/requirements-analysis.txt",
        "config/session-source-provenance.json", "config/session-runner-provenance.json",
        "scripts/bootstrap_rpc.py", "scripts/bootstrap_construction.py", "scripts/bootstrap_sessions.py",
        "scripts/run_sessions.py", "scripts/analyze_sessions.py", "scripts/verify_sessions.py",
        "tests/sessions/test_analysis.py", "tests/sessions/test_recovery.py"))
    return sorted(selected)


def python_signatures():
    result = {}
    for name, package in (("finsy", "finsy"), ("analysis", "numpy")):
        code = (f"import {package} as package; import json,platform; "
                "print(json.dumps(dict(package=package.__file__,version=package.__version__,python=platform.python_version())))")
        result[name] = json.loads(subprocess.check_output([str(RUNTIME / name / "bin/python"), "-c", code],
                                                        env=environment(), text=True))
    source = RUNTIME / "sources/finsy"
    if not Path(result["finsy"]["package"]).resolve().is_relative_to(source):
        raise ValueError("Finsy import is outside the pinned source")
    code = ("import grpc,google.protobuf,json;from google.protobuf.internal import api_implementation;"
            "print(json.dumps(dict(grpc=grpc.__version__,protobuf=google.protobuf.__version__,backend=api_implementation.Type())))")
    result["finsy"].update(json.loads(subprocess.check_output([str(RUNTIME / "finsy/bin/python"), "-c", code],
                                                           env=environment(), text=True)))
    if result["analysis"]["version"] != "1.26.4" or result["finsy"]["backend"] != "upb":
        raise ValueError("Unexpected session analysis environment")
    return result


def check_setup():
    check_provenance()
    record = json.loads(SETUP.read_text())
    for field in ("input_sha256", "generated_sha256"):
        for name, expected in record[field].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f"Session setup changed: {name}. Run bootstrap again.")
    for name, expected in record["source_inventory"].items():
        if inventory(RUNTIME / "sources" / name) != expected:
            raise ValueError(f"Dependency source changed: {name}")
    if python_signatures() != record["python_signatures"]:
        raise ValueError("Session Python environment changed")
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
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="Optional finsy/ and analysis/ wheel directories")
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
    output = ROOT / "results/session-setup"
    output.mkdir(parents=True, exist_ok=True)
    log = output / "bootstrap.log"
    log.write_text("")
    env = environment()
    env.pop("PYTHONOPTIMIZE", None)
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    sources = {item["name"]: install_source(item) for item in lock["sources"]}
    original = json.loads((ROOT / "data/sessions/environment.json").read_text())["versions"]
    sdk = {name: value for name, value in sources["p4runtime-go-controller"].items()
           if name.endswith(".go") or Path(name).name in ("go.mod", "go.sum")}
    if sdk != original["sdk_files_sha256"]:
        raise ValueError("SDK source differs from the archived session environment")
    packages = {name: install_python(name, args.wheelhouse, env, log) for name in ("finsy", "analysis")}
    goroot = command(["go", "env", "GOROOT"], ROOT, env, log)
    env["PATH"] = str(Path(goroot) / "bin") + os.pathsep + env["PATH"]
    go_version = command(["go", "version"], ROOT, env, log)
    if lock["go_toolchain"] not in go_version:
        raise ValueError(go_version)
    command([RUNTIME / "finsy/bin/python", SESSIONS / "compile.py"], ROOT, env, log)
    (SESSIONS / "bin").mkdir(exist_ok=True)
    for name in ("sdk", "session-sdk"):
        folder = SESSIONS / name
        command(["go", "vet", "-mod=readonly", "./..."], folder, env, log)
        for race in (False, True):
            argv = ["go", "build", "-mod=readonly"] + (["-race"] if race else [])
            command(argv + ["-o", SESSIONS / "bin" / (name + ("-race" if race else "")), "."], folder, env, log)
        command(["go", "version", "-m", SESSIONS / "bin" / name], ROOT, env, log)
    for pattern in ("test_analysis.py", "test_recovery.py"):
        command([RUNTIME / "finsy/bin/python", "-m", "unittest", "discover", "-s", ROOT / "tests/sessions", "-p", pattern, "-v"], ROOT, env, log)
    command([RUNTIME / "finsy/bin/python", "-m", "unittest", "discover", "-s", SESSIONS / "tests", "-v"], ROOT, env, log)
    generated = sorted(path for name in ("fixtures", "bin") for path in (SESSIONS / name).rglob("*")
                       if path.is_file() and "__pycache__" not in path.parts)
    record = dict(completed_local=datetime.now().astimezone().isoformat(), dependencies=lock,
                  source_inventory=sources, python_environments=packages, python_signatures=python_signatures(),
                  go=go_version, p4c=command(["p4c-bm2-ss", "--version"], ROOT, env, log),
                  bmv2=command(["simple_switch_grpc", "--version"], ROOT, env, log),
                  input_sha256={str(path.relative_to(ROOT)): digest(path) for path in input_files()},
                  generated_sha256={str(path.relative_to(ROOT)): digest(path) for path in generated},
                  go_vet_modules=2, normal_and_race_binaries=4, original_evidence_and_close_tests=15)
    SETUP.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(dict(completed=True, modules=2, binaries=4, tests=15)))


if __name__ == "__main__":
    main()
