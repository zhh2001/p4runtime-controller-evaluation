"""Verify newly executed batch-write retries with both archived decoders."""

import argparse
from datetime import datetime
import itertools
import json
from pathlib import Path
import subprocess

from bootstrap_retries import ROOT, RUNTIME, TARGETS, check_setup, digest, environment, selection
from retry_checks import analyze
from verify_retries import json_lines, require, summaries, verify_messages


def verify_coverage(folder, target, mode, setup):
    manifest = json.loads((folder / "manifest.json").read_text())
    plan, cases, profiles, repetitions = selection(target, mode)
    require(manifest["completed"] and manifest["mode"] == target and manifest["plan"] == plan and
            manifest["smoke"] == (mode == "smoke") and manifest["race"] == (mode == "race") and
            manifest["profiles"] == profiles and manifest["repetitions"] == repetitions and
            manifest["case_ids"] == [case["id"] for case in cases], "Incomplete or unexpected retry run settings")
    rows = json_lines((folder / "observations.jsonl").read_bytes())
    records = json_lines((folder / "rpc.jsonl").read_bytes())
    jobs = list(itertools.product(range(1, repetitions + 1), profiles, cases))
    require(len(rows) == manifest["observations"] == len(jobs), "Missing retry trials")
    require([record["sequence"] for record in records] == list(range(len(records))) and
            all(record["kind"] in ("write", "read") for record in records), "Missing or reordered retry RPC event")
    covered = []
    expected_logs = set()
    for row, (repetition, profile, case) in zip(rows, jobs):
        token = f"{repetition}|{profile}|{case['id']}"
        require((row["repetition"], row["profile"], row["case_id"], row["mode"], row["token"], row["case"]) ==
                (repetition, profile, case["id"], target, token, case), "Retry trial identity or order differs")
        require(1 <= len(row["steps"]) <= plan["max_attempts"] and
                [step["attempt"] for step in row["steps"]] == list(range(1, len(row["steps"]) + 1)),
                "Invalid retry attempt sequence")
        require(row["rpc_sequences"] == [record["sequence"] for record in records if record["token"] == token],
                "Retry trial RPC sequence differs")
        covered.extend(row["rpc_sequences"])
        directory = folder / token.replace("|", "__")
        logs = [directory / ("driver." + suffix) for suffix in ("jsonl", "stderr")]
        expected_logs.update(logs)
        require("DATA RACE" not in logs[1].read_text(), "Go race report")
        output = json_lines(logs[0].read_bytes())
        require(output[0] == dict(profile=profile, ready=True), "Driver readiness differs")
        expected = []
        for step in row["steps"]:
            if not step["result"].get("watchdog_timeout"):
                expected.append(step["result"])
            if "application_read" in step:
                expected.append(step["application_read"])
        require(output[1:] == expected, "Driver output differs from observations")
        command = json.loads((directory / "command.json").read_text())
        suffix = "-race" if mode == "race" else ""
        if profile in ("sdk", "raw-current", "legacy", "raw-legacy"):
            stack = "sdk" if profile in ("sdk", "raw-current") else "legacy"
            expected_command = [str(ROOT / "experiments/retries/bin" / ("go-" + stack + suffix))]
        else:
            expected_command = [str(RUNTIME / ("finsy" if profile == "finsy" else "shell") / "bin/python"),
                                str(ROOT / "experiments/retries/python_driver.py")]
        require(command[:-4] == expected_command and command[-4:-1] == ["--profile", profile, "--address"] and
                command[-1].startswith("127.0.0.1:"), "Selected retry driver differs")
    require(covered == list(range(len(records))), "Unassigned retry RPC event")
    require({path for path in folder.glob("*/driver.*")} == expected_logs, "Driver log coverage differs")
    expected = {**setup["input_sha256"], **setup["generated_sha256"],
                "experiments/batch-retry/cases.json": digest(ROOT / "data/retries/fixtures/cases.json")}
    require(manifest["source_sha256"] == expected and manifest["versions"] == setup["versions"],
            "Retry snapshot inventory or versions differ from setup")
    for name, value in expected.items():
        require(digest(folder / "source" / name) == value, f"Retry source snapshot changed: {name}")
    inventory = json.loads((folder / "artifact-hashes.json").read_text())
    required = {"manifest.json", "observations.jsonl", "rpc.jsonl"}
    required.update(str(path.relative_to(folder)) for path in expected_logs)
    required.update("source/" + name for name in expected)
    require(required <= set(inventory), "Incomplete retry artifact inventory")
    for name, value in inventory.items():
        require((folder / name).resolve().is_relative_to(folder.resolve()) and digest(folder / name) == value,
                f"Retry artifact changed: {name}")
    if target == "bmv2":
        require("target-processes.json" in inventory, "Missing target process inventory")
        processes = json.loads((folder / "target-processes.json").read_text())
        require(len(processes) == 1 and all("exit_code" in process and process["stop_ns"] > process["start_ns"]
                                           for process in processes), "Target stop record missing")
        for process in processes:
            ipc = process["command"][process["command"].index("--notifications-addr") + 1]
            require(ipc.startswith("ipc:///tmp/p4runtime-retries-") and not Path(ipc[6:]).exists(),
                    "Owned target IPC file remains")
    verify_messages(ROOT, rows, records)
    return rows


def verify(output):
    state = json.loads((output / "state.json").read_text())
    require(state["complete"] and state["targets"] and len(state["targets"]) == len(set(state["targets"])) and
            set(state["targets"]) <= set(TARGETS) and state["mode"] in ("smoke", "race", "full"), "Incomplete retry run")
    setup = check_setup()
    runs = []
    for target in state["targets"]:
        folder = output / target
        rows = verify_coverage(folder, target, state["mode"], setup)
        primary = analyze.analyze(folder)
        command = [str(RUNTIME / "finsy/bin/python"), "-I", str(ROOT / "scripts/retry_checks/secondary.py"), str(folder)]
        result = subprocess.run(command, env=environment(), capture_output=True, text=True)
        (folder / "secondary.log").write_text(result.stdout + result.stderr)
        require(result.returncode == 0, f"Independent retry verification failed: {target}. See {folder / 'secondary.log'}")
        runs.append((target, primary, rows))
    summaries(output, runs)
    keys = ("trials", "completed", "blocked", "watchdog_timeout", "write_requests", "update_attempts",
            "successful_updates", "failed_updates", "application_reads")
    counts = {key: sum(result["counts"].get(key, 0) for _, result, _ in runs) for key in keys}
    require(counts["trials"] == state["completed"], "Retry trial total differs")
    check_setup()
    report = dict(verified_local=datetime.now().astimezone().isoformat(), mode=state["mode"], targets=state["targets"],
                  counts=counts, source_snapshot_verified=True, independent_wire_decoder_passed=True)
    (output / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(verified=True, **counts)))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so evidence checks remain enabled.")
    verify(args.run.resolve())


if __name__ == "__main__":
    main()
