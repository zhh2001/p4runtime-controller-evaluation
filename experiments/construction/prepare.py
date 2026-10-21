"""Build the construction drivers and check their semantics and ownership."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime

from fixture import ROOT
from oracle import verify


REPOSITORY = ROOT.parents[1]
PYTHONS = {name: REPOSITORY / ".runtime" / name / "bin/python" for name in ("finsy", "shell")}


def run(command, output, env, cwd=ROOT):
    result = subprocess.run([str(value) for value in command], cwd=cwd, env=env, capture_output=True, text=True)
    output.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Command returned {result.returncode}. See {output}")
    return result.stdout


def main():
    if not __debug__:
        raise ValueError("Run without -O so semantic checks remain enabled")
    output = REPOSITORY / "results/construction-setup" / ("preflight-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    (ROOT / "bin").mkdir(exist_ok=True)
    env = dict(os.environ, CONSTRUCTION_ROOT=str(ROOT), GOMAXPROCS="1")
    env.pop("CONSTRUCTION_SAMPLES", None)
    checks = {}
    for stack in ("sdk", "legacy"):
        folder = ROOT / ("go-" + stack)
        sample = output / (stack + "-samples.json")
        run(["go", "test", "-mod=readonly", "-count=1", "-v", "./..."], output / (stack + "-test.log"),
            dict(env, CONSTRUCTION_SAMPLES=str(sample)), folder)
        checks[stack] = verify(sample)
        run(["go", "test", "-mod=readonly", "-race", "-count=1", "./..."], output / (stack + "-race.log"), env, folder)
        run(["go", "vet", "-mod=readonly", "./..."], output / (stack + "-vet.log"), env, folder)
        run(["go", "test", "-mod=readonly", "-c", "-o", ROOT / "bin" / (stack + ".test")],
            output / (stack + "-build.log"), env, folder)
        run(["go", "version", "-m", ROOT / "bin" / (stack + ".test")], output / (stack + "-build-info.txt"), env)
    for profile, python in PYTHONS.items():
        sample = output / (profile + "-samples.json")
        run([python, ROOT / "python_driver.py", "--profile", profile, "--check", sample],
            output / (profile + "-test.log"), env)
        checks[profile] = verify(sample)
    expected = {"sdk": 1536, "legacy": 1536, "finsy": 1024, "shell": 1024}
    if checks != expected:
        raise ValueError(f"Unexpected semantic sample counts: {checks}")
    run([PYTHONS["finsy"], "-m", "unittest", "discover", "-s", ROOT / "tests", "-v"],
        output / "oracle-tests.log", dict(env, CONSTRUCTION_PREFLIGHT=str(output)))
    files = [path for path in ROOT.rglob("*") if path.is_file() and "__pycache__" not in path.parts
             and path.name != "preflight.json"]
    verification = dict(created_local=datetime.now().astimezone().isoformat(), semantic_samples=checks,
                        total_semantic_samples=sum(checks.values()), ownership_checks=True,
                        go_race_and_vet_passed=True,
                        files_sha256={str(path.relative_to(REPOSITORY)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in files})
    (output / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    (ROOT / "preflight.json").write_text(json.dumps({"path": str(output)}, indent=2) + "\n")
    print(json.dumps({"output": str(output), "checked": sum(checks.values())}))


if __name__ == "__main__":
    main()
