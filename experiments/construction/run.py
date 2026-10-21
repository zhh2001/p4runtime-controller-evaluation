"""Run independent local-construction measurements in randomized blocks."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
from bootstrap_construction import RUNTIME, check_setup, environment as benchmark_environment

PYTHONS = {name: RUNTIME / name / "bin/python" for name in ("finsy", "shell")}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def check_versions():
    setup = check_setup()
    sources = {item["name"]: item for item in setup["dependencies"]["sources"]}
    hashes = {name: value for name, value in setup["source_inventory"]["p4runtime-go-controller"].items()
              if name.endswith(".go") or Path(name).name in ("go.mod", "go.sum")}
    assert len(hashes) == 148
    return dict(sdk_commit=sources["p4runtime-go-controller"]["commit"], sdk_files_sha256=hashes,
                others={name: item["commit"] for name, item in sources.items() if name != "p4runtime-go-controller"})


def snapshot(output):
    setup = check_setup()
    hashes = dict(setup["input_sha256"], **setup["generated_sha256"])
    for name, expected in hashes.items():
        path = REPOSITORY / name
        assert digest(path) == expected, name
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return hashes


def host_state():
    return {name: Path("/proc", name).read_text() for name in ("stat", "meminfo", "loadavg")}


def jobs(plan):
    result = [dict(kind="entry", profile=p, workload=w, mode=m, component="")
              for p in plan["profiles"] for w in plan["workloads"] for m in plan["modes"][p]]
    for component in plan["components_sdk_only"]:
        name, _, workload = component.partition("/")
        result.append(dict(kind="component", profile="sdk", workload=workload or "exact",
                           mode="", component=name))
    assert len(result) == 27
    return result


def run_job(job, seconds, cpu, output, number, env):
    python = job["profile"] in PYTHONS
    if python:
        command = [str(PYTHONS[job["profile"]]), str(ROOT / "python_driver.py"),
                   "--profile", job["profile"], "--workload", job["workload"],
                   "--mode", job["mode"], "--seconds", str(seconds)]
    else:
        stack = "legacy" if job["profile"] in ("legacy", "raw-legacy") else "sdk"
        if job["kind"] == "entry":
            segments = ["BenchmarkConstruct", job["profile"], job["workload"], job["mode"]]
        else:
            segments = ["BenchmarkComponents", job["component"]]
            if job["component"] != "encode_exact32":
                segments.append(job["workload"])
        pattern = "/".join("^" + re.escape(s) + "$" for s in segments)
        command = [str(ROOT / "bin" / (stack + ".test")), "-test.run=^$", "-test.bench=" + pattern,
                   "-test.benchtime=" + str(round(seconds * 1e9)) + "ns", "-test.count=1",
                   "-test.cpu=1", "-test.benchmem", "-test.timeout=2m"]
    before = host_state()
    started = time.monotonic_ns()
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
                            preexec_fn=lambda: os.sched_setaffinity(0, {cpu}))
    duration = time.monotonic_ns() - started
    (output / f"{number:03}-stdout.txt").write_text(result.stdout)
    (output / f"{number:03}-stderr.txt").write_text(result.stderr)
    (output / f"{number:03}-command.json").write_text(json.dumps(command, indent=2) + "\n")
    (output / f"{number:03}-host.json").write_text(json.dumps(dict(before=before, after=host_state()), indent=2) + "\n")
    assert result.returncode == 0, f"condition {number}: {result.stdout[-500:]} {result.stderr[-500:]}"
    if python:
        metrics = json.loads(result.stdout)
    else:
        lines = [line.split() for line in result.stdout.splitlines() if line.startswith("Benchmark")]
        assert len(lines) == 1, lines
        fields = lines[0]
        units = {fields[i + 1]: float(fields[i]) for i in range(2, len(fields), 2)}
        metrics = dict(iterations=int(fields[1]), elapsed_ns=int(units["elapsed-ns"]),
                       ns_per_op=units["elapsed-ns"] / int(fields[1]),
                       reported_ns_per_op=units["ns/op"], bytes_per_op=units["B/op"],
                       allocs_per_op=units["allocs/op"], timing_basis="WSL CLOCK_MONOTONIC")
    return dict(**job, sequence=number, command=command, exit_code=result.returncode,
                process_duration_ns=duration, cpu=cpu, metrics=metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so benchmark checks remain enabled.")
    plan = json.loads((ROOT / "formal-plan.json").read_text())
    if args.smoke:
        plan["repetitions"] = 1
        plan["seconds_per_measurement"] = 0.01
    preflight = Path(json.loads((ROOT / "preflight.json").read_text())["path"])
    checked = json.loads((preflight / "verification.json").read_text())
    for name, expected in checked["files_sha256"].items():
        assert digest(REPOSITORY / name) == expected, f"preflight input changed: {name}"
    versions = check_versions()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or REPOSITORY / "results/construction" / (("smoke-" if args.smoke else "formal-") + stamp)
    output.mkdir(parents=True, exist_ok=False)
    available = sorted(os.sched_getaffinity(0))
    cpu = available[0]
    manifest = dict(started_local=datetime.now().astimezone().isoformat(), plan=plan, smoke=args.smoke,
                    preflight=str(preflight), preflight_sha256=digest(preflight / "verification.json"),
                    source_sha256=snapshot(output), versions=versions, completed=False,
                    completed_measurements=0, affinity_cpu=cpu, available_cpus=available,
                    pid=os.getpid(), failures=[])
    environment = dict(kernel=platform.uname()._asdict(), cpu_info=Path("/proc/cpuinfo").read_text(),
                       memory=Path("/proc/meminfo").read_text(), go_env={k: os.environ.get(k) for k in ("GOGC", "GOMEMLIMIT")},
                       affinity_cpu=cpu, host_start=host_state())
    for profile, python in PYTHONS.items():
        code = "import sys,platform,grpc,google.protobuf; from google.protobuf.internal import api_implementation; "
        code += ("import finsy as package; " if profile == "finsy" else "import p4runtime_sh as package; ")
        code += "import json; print(json.dumps(dict(python=platform.python_version(),executable=sys.executable,package=package.__file__,grpc=grpc.__version__,protobuf=google.protobuf.__version__,protobuf_backend=api_implementation.Type())))"
        environment[profile] = json.loads(subprocess.check_output([str(python), "-c", code], text=True))
        assert str(RUNTIME / "sources" / ("finsy" if profile == "finsy" else "p4runtime-shell")) in environment[profile]["package"]
    (output / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")

    def save():
        target = output / "manifest.json"
        temporary = output / "manifest.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(target)

    rng = random.Random(plan["seed"])
    schedule = []
    for repetition in range(1, plan["repetitions"] + 1):
        block = jobs(plan)
        rng.shuffle(block)
        schedule.extend(dict(**job, repetition=repetition) for job in block)
    manifest["schedule"] = schedule
    save()
    env = dict(benchmark_environment(), CONSTRUCTION_ROOT=str(ROOT))
    try:
        with (output / "measurements.jsonl").open("w", buffering=1) as records:
            for number, job in enumerate(schedule, 1):
                row = run_job(job, plan["seconds_per_measurement"], cpu, output, number, env)
                row["repetition"] = job["repetition"]
                records.write(json.dumps(row) + "\n")
                manifest["completed_measurements"] = number
                save()
                print(json.dumps(dict(output=str(output), completed=number, total=len(schedule), **job)), flush=True)
        for name, expected in manifest["source_sha256"].items():
            assert digest(REPOSITORY / name) == expected, f"source changed during measurement: {name}"
        manifest.update(completed=True, completed_local=datetime.now().astimezone().isoformat())
    except BaseException as error:
        manifest["failures"].append(dict(type=type(error).__name__, message=str(error)))
        raise
    finally:
        save()
    print(json.dumps(dict(output=str(output), complete=True)), flush=True)


if __name__ == "__main__":
    main()
