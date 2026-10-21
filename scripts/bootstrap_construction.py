"""Install pinned clients and validate the offline construction benchmarks."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import venv

from bootstrap_rpc import ROOT, RUNTIME, command, digest, environment as rpc_environment
from bootstrap_rpc import install_source, inventory


CONSTRUCTION = ROOT / "experiments/construction"
SETUP = RUNTIME / "construction-setup.json"


def environment():
    env = rpc_environment()
    env["GOMAXPROCS"] = "1"
    env.pop("PYTHONOPTIMIZE", None)
    env.pop("CONSTRUCTION_SAMPLES", None)
    return env


def check_provenance():
    provenance = json.loads((ROOT / "config/construction-source-provenance.json").read_text())
    dataset = json.loads((ROOT / "data/construction/manifest.json").read_text())
    if (provenance["source_run"], provenance["source_manifest_sha256"]) != (
            dataset["source_run"], dataset["source_manifest_sha256"]):
        raise ValueError("Construction source run differs from the dataset")
    for name, record in provenance["files"].items():
        if digest(CONSTRUCTION / name) != record["export_sha256"]:
            raise ValueError(f"Construction source changed: {name}")
        if record["unchanged"] and record["formal_sha256"] != record["export_sha256"]:
            raise ValueError(f"Construction source correspondence differs: {name}")
        if record.get("source_file", "").startswith("source/experiments/construction/"):
            original = dataset["benchmark_sources_sha256"].get(name)
            if original is not None and original != record["formal_sha256"]:
                raise ValueError(f"Frozen source hash differs: {name}")
    for name in ("inputs.json", "construct.p4info.txt"):
        if (CONSTRUCTION / "fixtures" / name).read_bytes() != (ROOT / "data/construction/fixtures" / name).read_bytes():
            raise ValueError(f"Construction fixture differs: {name}")
    if (CONSTRUCTION / "formal-plan.json").read_bytes() != (ROOT / "data/construction/plan.json").read_bytes():
        raise ValueError("Construction plan differs from the archived plan")
    for stack in ("go-sdk", "go-legacy"):
        expected = {"bench_test.go": "bench_test.go",
                    "native_test.go": "sdk.go" if stack == "go-sdk" else "legacy.go"}
        if stack == "go-sdk":
            expected["components_test.go"] = "components_test.go"
        for target, source in expected.items():
            if (CONSTRUCTION / stack / target).read_bytes() != (CONSTRUCTION / source).read_bytes():
                raise ValueError(f"Shared construction source differs: {stack}/{target}")
    return provenance


def input_files(provenance):
    files = [CONSTRUCTION / name for name in provenance["files"]]
    files += [ROOT / name for name in (
        "config/dependencies.json", "config/requirements-finsy.txt", "config/requirements-shell.txt",
        "config/requirements-analysis.txt", "config/construction-source-provenance.json",
        "scripts/bootstrap_rpc.py", "scripts/bootstrap_construction.py", "scripts/run_construction.py",
        "scripts/recompute_rpc.py", "scripts/recompute_construction.py")]
    return sorted(set(files))


def check_setup():
    check_provenance()
    setup = json.loads(SETUP.read_text())
    for group in ("input_sha256", "generated_sha256"):
        for name, expected in setup[group].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f"Construction setup input or build product changed: {name}. Run bootstrap again.")
    for name, expected in setup["source_inventory"].items():
        if inventory(RUNTIME / "sources" / name) != expected:
            raise ValueError(f"Dependency source changed: {name}")
    preflight = ROOT / setup["preflight_record"]
    if digest(preflight) != setup["preflight_sha256"]:
        raise ValueError("Construction preflight record changed")
    return setup


def install_python(name, wheelhouse, env, log):
    location = RUNTIME / name
    if not location.exists():
        venv.EnvBuilder(with_pip=True).create(location)
    python = location / "bin/python"
    argv = [python, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
            "--no-deps", "-r", ROOT / f"config/requirements-{name}.txt"]
    if wheelhouse:
        argv += ["--no-index", "--find-links", wheelhouse.resolve() / name]
    command(argv, ROOT, env, log)
    if name in ("finsy", "shell"):
        source = "finsy" if name == "finsy" else "p4runtime-shell"
        site = command([python, "-c", "import site; print(site.getsitepackages()[0])"], ROOT, env, log)
        (Path(site) / "benchmark-source.pth").write_text(str(RUNTIME / "sources" / source) + "\n")
        backend = command([python, "-c", "from google.protobuf.internal import api_implementation; "
                           "print(api_implementation.Type())"], ROOT, env, log)
        if backend != ("upb" if name == "finsy" else "python"):
            raise ValueError(f"Unexpected protobuf backend for {name}: {backend}")
    command([python, "-m", "pip", "check"], ROOT, env, log)
    return json.loads(command([python, "-m", "pip", "list", "--format=json"], ROOT, env, log))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="Optional finsy/, shell/ and analysis/ wheel directories")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so benchmark checks remain enabled.")
    if sys.version_info[:2] != (3, 12) or platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("Use CPython 3.12 on Linux x86_64.")
    for name in ("go", "cc"):
        if shutil.which(name) is None:
            parser.error(f"Install {name} and put it on PATH.")
    provenance = check_provenance()
    RUNTIME.mkdir(exist_ok=True)
    state = ROOT / "results/construction-setup"
    state.mkdir(parents=True, exist_ok=True)
    log = state / "bootstrap.log"
    log.write_text("")
    env = environment()
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    sources = {item["name"]: install_source(item) for item in lock["sources"]}
    versions = {name: install_python(name, args.wheelhouse, env, log) for name in ("finsy", "shell", "analysis")}
    go_version = command(["go", "version"], ROOT, env, log)
    if lock["go_toolchain"] not in go_version:
        raise ValueError(go_version)
    goroot = command(["go", "env", "GOROOT"], ROOT, env, log)
    env["PATH"] = str(Path(goroot) / "bin") + os.pathsep + env["PATH"]
    command([RUNTIME / "finsy/bin/python", CONSTRUCTION / "prepare.py"], ROOT, env, log)
    preflight = Path(json.loads((CONSTRUCTION / "preflight.json").read_text())["path"]) / "verification.json"
    checked = json.loads(preflight.read_text())
    if checked["total_semantic_samples"] != 5120 or not checked["ownership_checks"] or not checked["go_race_and_vet_passed"]:
        raise ValueError("Construction preflight did not complete")
    result = dict(completed_local=datetime.now().astimezone().isoformat(), python=sys.version, go=go_version,
                  dependencies=lock, python_environments=versions, source_inventory=sources,
                  input_sha256={str(path.relative_to(ROOT)): digest(path) for path in input_files(provenance)},
                  generated_sha256={str(path.relative_to(ROOT)): digest(path)
                                    for path in sorted((CONSTRUCTION / "bin").iterdir()) if path.is_file()},
                  preflight_record=str(preflight.relative_to(ROOT)), preflight_sha256=digest(preflight),
                  semantic_samples=checked["total_semantic_samples"], go_race_and_vet_passed=True,
                  driver_tests_passed=True)
    SETUP.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"completed": True, "setup": str(SETUP), "semantic_samples": 5120}))


if __name__ == "__main__":
    main()
