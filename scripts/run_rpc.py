"""Run the seven RPC workloads with the pinned controller clients."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import subprocess

from bootstrap_rpc import ROOT, RUNTIME, PERF, digest, environment, inventory


def check_setup():
    setup = json.loads((RUNTIME / "setup.json").read_text())
    for group in ("input_sha256", "generated_sha256"):
        for name, expected in setup[group].items():
            if digest(ROOT / name) != expected:
                raise ValueError(f"Setup input or build product changed: {name}. Run bootstrap again.")
    for name, expected in setup["source_inventory"].items():
        if inventory(RUNTIME / "sources" / name) != expected:
            raise ValueError(f"Dependency source changed: {name}")
    provenance = json.loads((ROOT / "config/source-provenance.json").read_text())
    for name, record in provenance["files"].items():
        if digest(PERF / name) != record["export_sha256"]:
            raise ValueError(f"Benchmark source changed: {name}")
    return setup


def run_logged(argv, env, log):
    with log.open("w") as stream:
        stream.write(json.dumps([str(value) for value in argv]) + "\n")
        stream.flush()
        subprocess.run([str(value) for value in argv], cwd=ROOT, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config = json.loads((ROOT / "config/rpc.json").read_text())
    parser.add_argument("--points", nargs="+", choices=["all", *[item["id"] for item in config["points"]]],
                        default=["default"])
    parser.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(set(args.points)) != len(args.points) or ("all" in args.points and len(args.points) != 1):
        parser.error("Choose distinct point names, or use --points all.")
    output = (args.output or ROOT / "results" /
              ("rpc-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if output.is_relative_to(ROOT / "data"):
        parser.error("Output must be outside data/.")
    setup = check_setup()
    points = [item for item in config["points"] if "all" in args.points or item["id"] in args.points]
    mode = config["modes"][args.mode]
    env = environment()
    # The timed runs use the ordinary binaries. Race builds are for validation.
    env.pop("P4PERF_SDK_BINARY", None)
    env.pop("P4PERF_LEGACY_BINARY", None)
    output.mkdir(parents=True, exist_ok=False)
    host = {"started_local": datetime.now().astimezone().isoformat(),
            "platform": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "cpuinfo": Path("/proc/cpuinfo").read_text(), "meminfo": Path("/proc/meminfo").read_text()}
    execution = dict(mode=args.mode, configuration=mode, points=points,
                     setup=setup, host=host, completed_points=[])
    (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")
    python = RUNTIME / "finsy/bin/python"
    for point in points:
        directory = output / point["id"]
        print(f"Running {point['id']}: N={point['n']} B={point['batch']} C={point['concurrency']}", flush=True)
        argv = [python, PERF / "run.py", "--output", directory,
                "--repetitions", mode["repetitions"], "--warmup", mode["warmup_seconds"],
                "--seconds", mode["measurement_seconds"], "--configurations",
                f"{point['n']}:{point['batch']}:{point['concurrency']}"]
        try:
            run_logged(argv, env, output / (point["id"] + "-run.log"))
            run_logged([python, PERF / "analyze.py", directory], env,
                       output / (point["id"] + "-audit.log"))
            run_logged([python, PERF / "summarize.py", directory], env,
                       output / (point["id"] + "-summary.log"))
        except (subprocess.CalledProcessError, KeyboardInterrupt) as error:
            execution["failure"] = {"point": point["id"], "error": str(error)}
            (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")
            raise
        execution["completed_points"].append(point["id"])
        (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")
        print(f"Verified {point['id']}", flush=True)
    execution["completed_local"] = datetime.now().astimezone().isoformat()
    (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")
    print(json.dumps({"completed": True, "mode": args.mode, "output": str(output)}))


if __name__ == "__main__":
    main()
