"""Verify archived E3 events, session outcomes and statistical summaries."""

import argparse
from datetime import datetime
import gzip
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys

from session_checks import analyze, secondary


ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("recovery", "mastership", "cancellation", "consumers")
TRIALS = dict(recovery=180, mastership=60, cancellation=240, consumers=360)
GENERATED = dict(recovery=("recovery-trials.csv",), mastership=(), cancellation=("outcomes.csv",),
                 consumers=("consumer-summary.csv", "rss-evaluation.jsonl"))
FIXTURES = ("keys.p4info.txt", "keys.p4", "stream.p4info.txt", "validation-samples.json",
            "recovery-validation-sample.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def verify_inventory(root):
    manifest = json.loads((root / "data/sessions/manifest.json").read_text())
    require(manifest["schema_version"] == 1 and manifest["trials"] == 840 and
            manifest["repetitions"] == 30 and manifest["adapters"] == ["sdk", "finsy"] and
            manifest["groups"] == list(GROUPS), "Unexpected E3 manifest")
    require([(run["group"], run["trials"]) for run in manifest["runs"]] == list(TRIALS.items()),
            "Incorrect E3 run selection")
    expected = {"data/sessions/" + name for name in ("statistics.csv", "summary.json", "plan.json",
                                                     "selection.json", "environment.json")}
    expected.update("data/sessions/fixtures/" + name for name in FIXTURES)
    for group in GROUPS:
        expected.update("data/sessions/" + group + "/" + name for name in
                        ("observations.jsonl.gz", "rpc.jsonl.gz", "stderr.jsonl.gz", "run.json",
                         "target-processes.json", "recorded-verification.json", *GENERATED[group]))
    expected.update("figures/sessions/" + name + suffix for name in
                    ("session-recovery", "slow-consumer-tradeoff") for suffix in (".pdf", ".png"))
    require(set(manifest["files"]) == expected, "Incorrect E3 file inventory")
    actual = {str(path.relative_to(root)) for name in ("data/sessions", "figures/sessions")
              for path in (root / name).rglob("*") if path.is_file()}
    require(actual == expected | {"data/sessions/manifest.json"}, "Unexpected E3 archive file")
    for name, item in manifest["files"].items():
        path = root / name
        require(path.resolve().is_relative_to((root / name.split("/")[0] / "sessions").resolve()),
                "File outside E3 archive")
        data = path.read_bytes()
        require(len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                f"Size or SHA-256 differs: {name}")
        if name.endswith(".gz"):
            raw = gzip.decompress(data)
            require(len(raw) == item["uncompressed_bytes"] and
                    hashlib.sha256(raw).hexdigest() == item["uncompressed_sha256"] and
                    len(raw.splitlines()) == item["records"], f"Uncompressed record differs: {name}")
            if name.endswith(("observations.jsonl.gz", "rpc.jsonl.gz")):
                require(item["source_sha256"] == item["uncompressed_sha256"], f"Original bytes differ: {name}")
    provenance = json.loads((root / "config/session-source-provenance.json").read_text())
    for name, item in provenance["files"].items():
        require(digest(root / name) == item["export_sha256"], f"Analysis source changed: {name}")
        if item.get("unchanged"):
            require(item["source_sha256"] == item["export_sha256"], f"Source correspondence differs: {name}")
    return manifest


def configurations(plan, group):
    if group == "recovery":
        return [dict(adapter=adapter, mode=mode) for adapter, mode in
                itertools.product(plan["adapters"], plan["recovery_modes"])]
    if group == "mastership":
        return [dict(adapter=adapter) for adapter in plan["adapters"]]
    if group == "cancellation":
        return [dict(adapter=adapter, mode=mode) for adapter in plan["adapters"]
                for mode in plan["cancellation_modes"][adapter]]
    return [dict(adapter=adapter, path=path, delay_ms=delay) for adapter, path, delay in
            itertools.product(plan["adapters"], plan["consumer_paths"], plan["consumer_delays_ms"])]


def verify_coverage(rows, events, run, plan):
    group = run["kind"]
    require(run["complete"] and not run["smoke"] and not run["race"] and
            run["repetitions"] == 30 and run["adapters"] == ["sdk", "finsy"] and run["plan"] == plan,
            "Incorrect formal run settings")
    rng = random.Random(plan["seed"] + list(GROUPS).index(group))
    schedule = []
    for repetition in range(1, 31):
        block = configurations(plan, group)
        rng.shuffle(block)
        schedule.extend(dict(**job, repetition=repetition) for job in block)
    require(run["schedule"] == schedule and len(rows) == run["trials"] == TRIALS[group],
            "Missing trials or changed randomized schedule")
    require([event["sequence"] for event in events] == list(range(len(events))),
            "Missing, duplicate or reordered RPC event")
    previous = -1
    for row, job in zip(rows, schedule):
        require(all(row.get(key) == value for key, value in job.items()), "Trial identity or order differs")
        start, end = row["start_sequence"], row["end_sequence"]
        require(previous < start < end < len(events), "Overlapping or invalid trial event slice")
        first, last = events[start], events[end]
        require(first["event"] == "trial_started" and last["event"] == "trial_completed" and
                all(first.get(key) == value for key, value in job.items()) and
                last["adapter"] == job["adapter"], "Trial event markers differ")
        previous = end
        if group == "recovery":
            require(row["observation_window_seconds"] == plan["recovery_observation_seconds"], "Recovery window differs")
        if group == "consumers":
            require(row["sent"] == plan["packet_count"] and row["offered_rate_pps"] == plan["offered_rate_pps"] and
                    row["application_capacity"] ==
                    (plan["application_queue_capacity"] if row["path"] == "queued" else None) and
                    row["library_packet_queue_capacity"] ==
                    (plan["library_packet_queue_capacity_finsy"] if row["adapter"] == "finsy" else None),
                    "Consumer workload or queue capacity differs")


def prepare_run(root, output, selected, plan):
    group = selected["group"]
    source = root / "data/sessions" / group
    run = json.loads((source / "run.json").read_text())
    require(run["kind"] == group and run["source_manifest_sha256"] == selected["source_manifest_sha256"],
            "Run metadata differs from the catalog")
    folder = output / group
    folder.mkdir()
    for name in ("observations", "rpc"):
        (folder / (name + ".jsonl")).write_bytes(gzip.decompress((source / (name + ".jsonl.gz")).read_bytes()))
    rows, events = json_lines(folder / "observations.jsonl"), json_lines(folder / "rpc.jsonl")
    verify_coverage(rows, events, run, plan)
    logs = [json.loads(line) for line in gzip.decompress((source / "stderr.jsonl.gz").read_bytes()).splitlines()]
    expected_logs = {"-".join(str(job[key]) for key in ("adapter", "mode", "path", "delay_ms", "repetition")
                             if key in job) + ".stderr" for job in run["schedule"]}
    require(len(logs) == len(expected_logs) and {log["file"] for log in logs} == expected_logs,
            "Driver stderr coverage differs")
    for log in logs:
        require(Path(log["file"]).name == log["file"] and
                hashlib.sha256(log["text"].encode()).hexdigest() == log["text_sha256"], "Driver stderr hash differs")
        require("DATA RACE" not in log["text"], "Go race report in driver stderr")
        if not log["local_path_replaced"]:
            require(log["source_sha256"] == log["text_sha256"], "Original stderr hash differs")
        (folder / log["file"]).write_text(log["text"])
    processes = json.loads((source / "target-processes.json").read_text())
    require(processes == [process for row in rows for process in row.get("target_processes", [])] and
            all("exit_code" in process for process in processes), "Target process history differs")
    (folder / "target-processes.json").write_bytes((source / "target-processes.json").read_bytes())
    hashes = {}
    for name in FIXTURES:
        original = "experiments/session-formal/fixtures/" + name
        fixture = root / "data/sessions/fixtures" / name
        require(run["execution_sources_sha256"][original] == digest(fixture),
                f"Fixture differs from execution inventory: {group}/{name}")
        path = folder / "source" / original
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fixture.read_bytes())
        hashes[original] = digest(path)
    run["files_sha256"] = hashes
    (folder / "manifest.json").write_text(json.dumps(run, indent=2) + "\n")
    return folder


def run_check(script, output):
    command = [sys.executable, str(ROOT / "scripts/session_checks" / script), str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    (output / (script.removesuffix(".py") + ".log")).write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f"{script} failed. See its log in {output}.")


def verify(root, output):
    manifest = verify_inventory(root)
    require(not any(output.resolve().is_relative_to((root / name).resolve()) for name in ("data", "figures")),
            "Output must be outside the archived data and figures")
    output.mkdir(parents=True, exist_ok=False)
    plan = json.loads((root / "data/sessions/plan.json").read_text())
    selection = json.loads((root / "data/sessions/selection.json").read_text())
    require(selection["complete"] and selection["completed"] == 840 and selection["groups"] == list(GROUPS),
            "Incomplete E3 selection")
    (output / "state.json").write_text(json.dumps(selection, indent=2) + "\n")
    results = []
    for selected in manifest["runs"]:
        group = selected["group"]
        folder = prepare_run(root, output, selected, plan)
        primary = analyze.analyze(folder)
        recorded = json.loads((root / "data/sessions" / group / "recorded-verification.json").read_text())
        recorded.pop("source_snapshot_verified")
        require({key: value for key, value in primary.items() if key != "analysis_fixture_snapshot_verified"} == recorded,
                f"Recomputed evidence report differs: {group}")
        for name in GENERATED[group]:
            require((folder / name).read_bytes() == (root / "data/sessions" / group / name).read_bytes(),
                    f"Recomputed trial output differs: {group}/{name}")
        independent = secondary.check_group(folder)
        results.append(dict(group=group, primary=primary, secondary=independent))
    for script in ("extra_checks.py", "summarize.py", "verify_statistics.py"):
        run_check(script, output)
    require((output / "statistics.csv").read_bytes() == (root / "data/sessions/statistics.csv").read_bytes(),
            "Recomputed statistics differ")
    saved = json.loads((root / "data/sessions/summary.json").read_text())
    recomputed = json.loads((output / "summary.json").read_text())
    excluded = {"created_local", "inputs_sha256", "script_sha256"}
    require({key: value for key, value in saved.items() if key not in excluded} ==
            {key: value for key, value in recomputed.items() if key not in excluded}, "Recomputed outcomes differ")
    for run in manifest["runs"]:
        group = run["group"]
        for name in ("manifest.json", "observations.jsonl", "rpc.jsonl", "verification.json"):
            exported = {"manifest.json": "run.json", "verification.json": "recorded-verification.json"}.get(name, name + ".gz")
            item = manifest["files"]["data/sessions/" + group + "/" + exported]
            require(saved["inputs_sha256"][group + "/" + name] == item["source_sha256"],
                    f"Summary source binding differs: {group}/{name}")
    record = dict(verified_local=datetime.now().astimezone().isoformat(), trials=840,
                  packets_checked=sum(result["secondary"]["packets_checked"] for result in results), runs=results,
                  statistics=json.loads((output / "statistics-verification.json").read_text()),
                  raw_writes=json.loads((output / "extra-verification.json").read_text()),
                  manifest_sha256=digest(root / "data/sessions/manifest.json"),
                  analysis_provenance_sha256=digest(root / "config/session-source-provenance.json"))
    require(record["packets_checked"] == 108000 and record["statistics"]["summary_rows"] == 62 and
            record["statistics"]["numbers_checked"] == 496, "Incorrect E3 verification totals")
    (output / "verification.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/sessions")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so the evidence checks remain enabled.")
    result = verify(ROOT, args.output.resolve())
    print(json.dumps(dict(verified=True, trials=result["trials"], packets=result["packets_checked"], statistics_rows=62)))


if __name__ == "__main__":
    main()
