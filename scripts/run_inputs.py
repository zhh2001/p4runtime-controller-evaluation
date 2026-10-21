"""Run and verify E1 experiments on controlled services and owned BMv2 processes."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import platform
import subprocess

from bootstrap_inputs import ROOT, RUNTIME, INPUTS, GROUPS, check_setup, environment


def run_logged(argv, env, log):
    with log.open("w") as stream:
        stream.write(json.dumps([str(value) for value in argv]) + "\n")
        stream.flush()
        subprocess.run([str(value) for value in argv], cwd=ROOT, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "full", "race"), default="smoke")
    parser.add_argument("--group", choices=(*GROUPS, "all"), default="all")
    parser.add_argument("--target", choices=("controlled", "bmv2", "all"), default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so semantic checks remain enabled.")
    output = (args.output or ROOT / "results" / ("inputs-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if any(output.is_relative_to(ROOT / name) for name in ("data", "figures")):
        parser.error("Output must be outside the archived data and figures.")
    setup = check_setup()
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    env.pop("PYTHONOPTIMIZE", None)
    groups = GROUPS if args.group == "all" else (args.group,)
    targets = ("controlled", "bmv2") if args.target == "all" else (args.target,)
    execution = dict(started_local=datetime.now().astimezone().isoformat(), platform=platform.platform(),
                     mode=args.mode, groups=groups, targets=targets, setup=setup, completed=False, runs=[])
    record = output / "execution.json"
    def save():
        record.write_text(json.dumps(execution, indent=2) + "\n")
    save()
    try:
        for group in groups:
            for target in targets:
                print(f"Running {group} {target} {args.mode}", flush=True)
                folder = output / group / target
                argv = [RUNTIME / "finsy/bin/python", INPUTS / group / "run.py", "--mode", target, "--output", folder]
                if args.mode != "full":
                    argv.append("--" + args.mode)
                run_logged(argv, env, output / (group + "-" + target + "-run.log"))
                run_logged([RUNTIME / "finsy/bin/python", ROOT / "scripts/analyze_inputs.py", "--group", group, folder],
                           env, output / (group + "-" + target + "-analysis.log"))
                execution["runs"].append(json.loads((folder / "analysis.json").read_text()))
                save()
        check_setup()
        execution.update(completed=True, completed_local=datetime.now().astimezone().isoformat(),
                         observations=sum(run["observations"] for run in execution["runs"]))
    except BaseException as error:
        execution["failure"] = dict(type=type(error).__name__, message=str(error))
        raise
    finally:
        save()
    print(json.dumps(dict(completed=True, mode=args.mode, observations=execution["observations"], output=str(output))))


if __name__ == "__main__":
    main()
