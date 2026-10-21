"""Run and verify E3 recovery, mastership, cancellation and consumer scenarios."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import signal
import subprocess

from bootstrap_sessions import ROOT, RUNTIME, SESSIONS, check_setup, environment

GROUPS = ("recovery", "mastership", "cancellation", "consumers")


def run_logged(argv, env, log):
    with log.open("w") as stream:
        stream.write(json.dumps([str(arg) for arg in argv]) + "\n")
        stream.flush()
        process = subprocess.Popen([str(arg) for arg in argv], cwd=ROOT, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, argv)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "race", "full"), default="smoke")
    parser.add_argument("--groups", nargs="+", choices=GROUPS, default=list(GROUPS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so evidence checks remain enabled.")
    if len(set(args.groups)) != len(args.groups):
        parser.error("Select each group once.")
    output = (args.output or ROOT / "results" / ("sessions-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if any(output.is_relative_to(ROOT / name) for name in ("data", "figures")):
        parser.error("Output must be outside the archived data and figures.")
    setup = check_setup()
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    for name in ("PYTHONOPTIMIZE", "P4EXPERIMENT_SDK_BINARY", "P4EXPERIMENT_SESSION_BINARY"):
        env.pop(name, None)
    execution = dict(started_local=datetime.now().astimezone().isoformat(), mode=args.mode, groups=args.groups,
                     platform=platform.platform(), setup=setup, completed=False)
    record = output / "execution.json"
    record.write_text(json.dumps(execution, indent=2) + "\n")
    try:
        argv = [RUNTIME / "finsy/bin/python", SESSIONS / "run.py", "--output", output / "run", "--groups", *args.groups]
        if args.mode != "full":
            argv.append("--" + args.mode)
        run_logged(argv, env, output / "run.log")
        run_logged([RUNTIME / "finsy/bin/python", ROOT / "scripts/analyze_sessions.py", output / "run"],
                   env, output / "analysis.log")
        check_setup()
        analysis = json.loads((output / "run/analysis.json").read_text())
        execution.update(completed=True, completed_local=datetime.now().astimezone().isoformat(), analysis=analysis)
    except BaseException as error:
        execution["failure"] = dict(type=type(error).__name__, message=str(error))
        raise
    finally:
        record.write_text(json.dumps(execution, indent=2) + "\n")
    print(json.dumps(dict(completed=True, mode=args.mode, trials=analysis["trials"], output=str(output))))


if __name__ == "__main__":
    main()
