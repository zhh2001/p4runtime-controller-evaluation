"""Install the pinned RPC clients and build the benchmark drivers."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
PERF = ROOT / "experiments/performance"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            result[str(path.relative_to(directory))] = {"symlink": os.readlink(path)}
        elif path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(directory))] = digest(path)
    return result


def environment():
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    env = dict(os.environ, GOTOOLCHAIN=lock["go_toolchain"], GOWORK="off",
               PYTHONNOUSERSITE="1")
    for name in ("PYTHONPATH", "PYTHONHOME", "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"):
        env.pop(name, None)
    return env


def command(argv, cwd, env, log):
    args = [str(arg) for arg in argv]
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    with log.open("a") as stream:
        stream.write(json.dumps(args) + "\n" + result.stdout + result.stderr + "\n")
    if result.returncode:
        raise RuntimeError(f"Command returned {result.returncode}. See {log}")
    return result.stdout.strip()


def install_source(source):
    archive = RUNTIME / "downloads" / (source["name"] + ".tar.gz")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        request = urllib.request.Request(source["url"], headers={"User-Agent": "p4runtime-evaluation"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError(f"Archive hash differs: {source['name']}")
        archive.write_bytes(data)
    if digest(archive) != source["sha256"]:
        raise ValueError(f"Archive hash differs: {archive}")
    destination = RUNTIME / "sources" / source["name"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RUNTIME) as temporary:
        with tarfile.open(archive) as packed:
            packed.extractall(temporary, filter="data")
        roots = list(Path(temporary).iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError(f"Unexpected archive layout: {archive}")
        expected = inventory(roots[0])
        if destination.exists():
            if inventory(destination) != expected:
                raise ValueError(f"Source checkout differs: {destination}")
        else:
            shutil.copytree(roots[0], destination, symlinks=True)
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="Optional directory with finsy/ and shell/ wheels")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or platform.machine() != "x86_64" or platform.system() != "Linux":
        parser.error("Use CPython 3.12 on Linux x86_64.")
    for name in ("go", "p4c-bm2-ss", "simple_switch_grpc", "cc"):
        if shutil.which(name) is None:
            parser.error(f"Install {name} and put it on PATH.")
    RUNTIME.mkdir(exist_ok=True)
    state = ROOT / "results/setup"
    state.mkdir(parents=True, exist_ok=True)
    log = state / "bootstrap.log"
    log.write_text("")
    env = environment()
    lock = json.loads((ROOT / "config/dependencies.json").read_text())
    sources = {item["name"]: install_source(item) for item in lock["sources"]}
    versions = {}
    for name, source, backend in (("finsy", "finsy", "upb"), ("shell", "p4runtime-shell", "python")):
        location = RUNTIME / name
        if not location.exists():
            venv.EnvBuilder(with_pip=True).create(location)
        python = location / "bin/python"
        argv = [python, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
                "--no-deps", "-r", ROOT / f"config/requirements-{name}.txt"]
        if args.wheelhouse:
            argv += ["--no-index", "--find-links", args.wheelhouse.resolve() / name]
        command(argv, ROOT, env, log)
        site = command([python, "-c", "import site; print(site.getsitepackages()[0])"], ROOT, env, log)
        (Path(site) / "benchmark-source.pth").write_text(str(RUNTIME / "sources" / source) + "\n")
        command([python, "-m", "pip", "check"], ROOT, env, log)
        actual = command([python, "-c", "from google.protobuf.internal import api_implementation; "
                          "print(api_implementation.Type())"], ROOT, env, log)
        if actual != backend:
            raise ValueError(f"{name} protobuf backend: expected {backend}, got {actual}")
        versions[name] = {"protobuf_backend": actual, "packages": json.loads(command(
            [python, "-m", "pip", "list", "--format=json"], ROOT, env, log))}
    goroot = command(["go", "env", "GOROOT"], ROOT, env, log)
    env["PATH"] = str(Path(goroot) / "bin") + os.pathsep + env["PATH"]
    go_version = command(["go", "version"], ROOT, env, log)
    if lock["go_toolchain"] not in go_version:
        raise ValueError(go_version)
    command([RUNTIME / "finsy/bin/python", PERF / "prepare.py"], ROOT, env, log)
    (PERF / "bin").mkdir(exist_ok=True)
    for stack in ("go-sdk", "go-legacy"):
        for name in ("shared.go", "counts_test.go"):
            if (PERF / name).read_bytes() != (PERF / stack / name).read_bytes():
                raise ValueError(f"Shared source differs in {stack}: {name}")
        for race in (False, True):
            argv = ["go", "build", "-mod=readonly"]
            if race:
                argv += ["-race"]
            argv += ["-o", PERF / "bin" / (stack + ("-race" if race else "")), "."]
            command(argv, PERF / stack, env, log)
        command(["go", "vet", "-mod=readonly", "./..."], PERF / stack, env, log)
        command(["go", "test", "-race", "-mod=readonly", "./..."], PERF / stack, env, log)
    command([RUNTIME / "finsy/bin/python", "-m", "unittest", "discover",
             "-s", PERF / "tests", "-v"], ROOT, env, log)
    generated = [*sorted((PERF / "bin").iterdir()), *sorted((PERF / "fixtures").iterdir())]
    result = {
        "completed_local": datetime.now().astimezone().isoformat(),
        "python": sys.version, "go": go_version,
        "bmv2": command(["simple_switch_grpc", "--version"], ROOT, env, log),
        "p4c": command(["p4c-bm2-ss", "--version"], ROOT, env, log),
        "dependencies": lock, "python_environments": versions, "source_inventory": sources,
        "input_sha256": {str(path.relative_to(ROOT)): digest(path)
                         for path in sorted((ROOT / "config").iterdir()) if path.is_file()},
        "generated_sha256": {str(path.relative_to(ROOT)): digest(path)
                             for path in generated if path.is_file()},
        "go_vet_passed": True, "go_race_tests_passed": True, "driver_tests_passed": True,
    }
    (RUNTIME / "setup.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"completed": True, "setup": str(RUNTIME / "setup.json")}))


if __name__ == "__main__":
    main()
