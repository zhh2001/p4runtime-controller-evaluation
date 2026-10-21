"""Verify newly executed E3 groups and summarize complete 30-round runs."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import random
import subprocess

from bootstrap_sessions import ROOT, RUNTIME, SESSIONS, check_setup, digest, environment
from session_checks import analyze, secondary
from verify_sessions import GROUPS, configurations, json_lines, require


def verify_coverage(folder, state):
    manifest = json.loads((folder / "manifest.json").read_text())
    group, plan = manifest["kind"], manifest["plan"]
    require(group in state["groups"] and manifest["complete"], "Incomplete or unexpected session group")
    require(plan == json.loads((ROOT / "data/sessions/plan.json").read_text()), "Session plan differs from archive")
    require(not (manifest["race"] and manifest["smoke"]), "Conflicting session modes")
    adapters = ["sdk"] if manifest["race"] else plan["adapters"]
    repetitions = 1 if manifest["smoke"] or manifest["race"] else 30
    require(manifest["adapters"] == adapters and manifest["repetitions"] == repetitions,
            "Session profiles or repetitions differ")
    block_plan = dict(plan, adapters=adapters)
    rng = random.Random(plan["seed"] + list(GROUPS).index(group))
    schedule = []
    for repetition in range(1, repetitions + 1):
        block = configurations(block_plan, group)
        rng.shuffle(block)
        schedule.extend(dict(**job, repetition=repetition) for job in block)
    rows, events = json_lines(folder / "observations.jsonl"), json_lines(folder / "rpc.jsonl")
    require(manifest["schedule"] == schedule and len(rows) == manifest["trials"] == len(schedule),
            "Missing trials or changed randomized schedule")
    require([event["sequence"] for event in events] == list(range(len(events))), "Missing, duplicate or reordered event")
    previous = -1
    for row, job in zip(rows, schedule):
        require(all(row.get(key) == value for key, value in job.items()), "Trial identity or order differs")
        start, end = row["start_sequence"], row["end_sequence"]
        require(previous < start < end < len(events), "Invalid trial event slice")
        require(events[start]["event"] == "trial_started" and events[end]["event"] == "trial_completed" and
                all(events[start].get(key) == value for key, value in job.items()), "Trial event markers differ")
        previous = end
        if group == "recovery":
            require(row["observation_window_seconds"] == plan["recovery_observation_seconds"], "Recovery window differs")
        if group == "consumers":
            require(row["sent"] == plan["packet_count"] and row["offered_rate_pps"] == plan["offered_rate_pps"] and
                    row["application_capacity"] == (50 if row["path"] == "queued" else None) and
                    row["library_packet_queue_capacity"] == (50 if row["adapter"] == "finsy" else None),
                    "Consumer workload or queue capacity differs")
    expected_logs = {"-".join(str(job[key]) for key in ("adapter", "mode", "path", "delay_ms", "repetition")
                             if key in job) + ".stderr" for job in schedule}
    require({path.name for path in folder.glob("*.stderr")} == expected_logs, "Driver log coverage differs")
    require({path.name for path in folder.glob("*.driver.jsonl")} ==
            {name.removesuffix(".stderr") + ".driver.jsonl" for name in expected_logs}, "Driver output coverage differs")
    if group == "cancellation":
        require(manifest["backpressure_probe_seconds"] == 1.0, "Backpressure probe differs")
    require(not any("DATA RACE" in path.read_text() for path in folder.glob("*.stderr")), "Go race report")
    processes = json.loads((folder / "target-processes.json").read_text())
    require(processes == [process for row in rows for process in row.get("target_processes", [])] and
            all("exit_code" in process for process in processes), "Target process history differs")
    setup = check_setup()
    hashes = {**setup["input_sha256"], **setup["generated_sha256"]}
    require(manifest["files_sha256"] == hashes, "Session snapshot inventory differs from setup")
    for name, expected in hashes.items():
        require(digest(folder / "source" / name) == expected, f"Source snapshot changed: {name}")
    binary = "experiments/sessions/bin/sdk" + ("-race" if manifest["race"] else "")
    require(manifest["sdk_executable_sha256"] == hashes[binary], "Selected driver binary differs")
    return manifest


def run_check(name, output):
    python = RUNTIME / ("analysis/bin/python" if name == "verify_statistics.py" else "finsy/bin/python")
    result = subprocess.run([str(python), str(SESSIONS / name), str(output)], env=environment(), capture_output=True, text=True)
    (output / (name.removesuffix(".py") + ".log")).write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f"{name} failed. See its log in {output}.")


def verify(output):
    state = json.loads((output / "state.json").read_text())
    require(state["complete"] and state["groups"] and len(state["groups"]) == len(set(state["groups"])) and
            set(state["groups"]) <= set(GROUPS), "Incomplete session run")
    results, modes = [], set()
    for group in state["groups"]:
        folder = output / group
        manifest = verify_coverage(folder, state)
        modes.add("race" if manifest["race"] else "smoke" if manifest["smoke"] else "full")
        primary = analyze.analyze(folder)
        independent = secondary.check_group(folder)
        results.append(dict(group=group, trials=manifest["trials"], primary=primary, secondary=independent))
    require(len(modes) == 1 and sum(result["trials"] for result in results) == state["completed"], "Session totals differ")
    mode = modes.pop()
    run_check("extra_checks.py", output)
    statistics = None
    if mode == "full":
        run_check("summarize.py", output)
        if any(group != "cancellation" for group in state["groups"]):
            run_check("verify_statistics.py", output)
            statistics = json.loads((output / "statistics-verification.json").read_text())
    result = dict(verified_local=datetime.now().astimezone().isoformat(), mode=mode, trials=state["completed"],
                  runs=results, packets_checked=sum(item["secondary"]["packets_checked"] for item in results),
                  statistics=statistics, source_snapshot_verified=True,
                  raw_writes=json.loads((output / "extra-verification.json").read_text()))
    (output / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(verified=True, mode=mode, trials=state["completed"], packets=result["packets_checked"])))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so evidence checks remain enabled.")
    check_setup()
    verify(args.run.resolve())


if __name__ == "__main__":
    main()
