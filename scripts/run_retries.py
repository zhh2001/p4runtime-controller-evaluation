"""Run and verify controlled-service and BMv2 batch-write retry experiments."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import platform

from bootstrap_retries import ROOT, RUNTIME, RETRIES, TARGETS, check_setup, environment
from run_sessions import run_logged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "race", "full"), default="smoke")
    parser.add_argument("--targets", nargs="+", choices=TARGETS, default=list(TARGETS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so evidence checks remain enabled.")
    if len(set(args.targets)) != len(args.targets):
        parser.error("Select each target once.")
    output = (args.output or ROOT / "results" / ("retries-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if any(output.is_relative_to(ROOT / name) for name in ("data", "figures")):
        parser.error("Output must be outside the archived data and figures.")
    setup = check_setup()
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    env.pop("PYTHONOPTIMIZE", None)
    execution = dict(started_local=datetime.now().astimezone().isoformat(), mode=args.mode, targets=args.targets,
                     platform=platform.platform(), setup=setup, completed=False)
    state = dict(complete=False, targets=args.targets, mode=args.mode, completed=0)
    record = output / "execution.json"
    record.write_text(json.dumps(execution, indent=2) + "\n")
    try:
        (output / "run").mkdir()
        for target in args.targets:
            state["current_target"] = target
            (output / "run/state.json").write_text(json.dumps(state, indent=2) + "\n")
            argv = [RUNTIME / "finsy/bin/python", RETRIES / "run.py", "--mode", target,
                    "--output", output / "run" / target]
            if args.mode != "full":
                argv.append("--" + args.mode)
            run_logged(argv, env, output / (target + ".log"))
            manifest = json.loads((output / "run" / target / "manifest.json").read_text())
            state["completed"] += manifest["observations"]
        state.update(complete=True, current_target=None)
        (output / "run/state.json").write_text(json.dumps(state, indent=2) + "\n")
        run_logged([RUNTIME / "finsy/bin/python", ROOT / "scripts/analyze_retries.py", output / "run"],
                   env, output / "analysis.log")
        check_setup()
        analysis = json.loads((output / "run/analysis.json").read_text())
        execution.update(completed=True, completed_local=datetime.now().astimezone().isoformat(), analysis=analysis)
    except BaseException as error:
        execution["failure"] = dict(type=type(error).__name__, message=str(error))
        raise
    finally:
        record.write_text(json.dumps(execution, indent=2) + "\n")
    print(json.dumps(dict(completed=True, mode=args.mode, trials=analysis["counts"]["trials"], output=str(output))))


if __name__ == "__main__":
    main()
