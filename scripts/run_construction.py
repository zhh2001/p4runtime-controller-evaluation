"""Run and verify the fixed local table-entry construction workloads."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import subprocess

from bootstrap_construction import ROOT, RUNTIME, CONSTRUCTION, check_setup, environment


def run_logged(argv, env, log):
    with log.open("w") as stream:
        stream.write(json.dumps([str(value) for value in argv]) + "\n")
        stream.flush()
        subprocess.run([str(value) for value in argv], cwd=ROOT, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so workload checks remain enabled.")
    output = (args.output or ROOT / "results" /
              ("construction-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if any(output.is_relative_to(ROOT / name) for name in ("data", "figures")):
        parser.error("Output must be outside the archived data and figures.")
    setup = check_setup()
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    raw = output / "run"
    execution = dict(mode=args.mode, setup=setup,
                     host=dict(started_local=datetime.now().astimezone().isoformat(),
                               platform=platform.platform(), python=platform.python_version(),
                               cpu_count=os.cpu_count(), cpu_affinity=sorted(os.sched_getaffinity(0)),
                               cpuinfo=Path("/proc/cpuinfo").read_text(), meminfo=Path("/proc/meminfo").read_text()),
                     completed=False)
    record = output / "execution.json"
    record.write_text(json.dumps(execution, indent=2) + "\n")
    argv = [RUNTIME / "finsy/bin/python", CONSTRUCTION / "run.py", "--output", raw]
    if args.mode == "smoke":
        argv.append("--smoke")
    try:
        print(f"Running construction {args.mode}", flush=True)
        run_logged(argv, env, output / "run.log")
        analysis = [RUNTIME / "analysis/bin/python", CONSTRUCTION / "analyze.py", raw]
        if args.mode == "smoke":
            analysis.append("--check-only")
        run_logged(analysis, env, output / "analysis.log")
        if args.mode == "full":
            run_logged([RUNTIME / "analysis/bin/python", CONSTRUCTION / "verify_statistics.py", raw],
                       env, output / "crosscheck.log")
        execution["verification"] = json.loads((raw / "verification.json").read_text())
    except (subprocess.CalledProcessError, KeyboardInterrupt) as error:
        execution["failure"] = {"type": type(error).__name__, "message": str(error)}
        record.write_text(json.dumps(execution, indent=2) + "\n")
        raise
    execution.update(completed=True, completed_local=datetime.now().astimezone().isoformat())
    record.write_text(json.dumps(execution, indent=2) + "\n")
    print(json.dumps({"completed": True, "mode": args.mode, "output": str(output)}))


if __name__ == "__main__":
    main()
