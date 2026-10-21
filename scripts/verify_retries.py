"""Verify archived E4 batch writes, retry decisions and final table state."""

import argparse
import base64
import csv
from datetime import datetime
import gzip
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys

from finsy.proto import p4i, p4r, rpc_status
from google.protobuf import json_format, text_format

from retry_checks import analyze, model, policy


ROOT = Path(__file__).resolve().parents[1]
MODES = ("controlled", "bmv2")
TRIALS = dict(controlled=120, bmv2=72)
FIXTURES = {
    "fixtures/cases.json": "experiments/batch-retry/cases.json",
    "plan.json": "experiments/batch-retry/formal-plan.json",
    "fixtures/keys.p4": "experiments/bmv2-pilot/fixtures/keys.p4",
    "fixtures/keys.p4info.txt": "experiments/bmv2-pilot/fixtures/keys.p4info.txt",
}
GENERATED = ("counts.csv", "profile-outcomes.csv", "trials.csv")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_lines(data):
    return [json.loads(line) for line in data.splitlines()]


def archived(root, mode, name):
    return json_lines(gzip.decompress((root / "data/retries" / mode / (name + ".jsonl.gz")).read_bytes()))


def verify_inventory(root):
    archive = root / "data/retries"
    manifest = json.loads((archive / "manifest.json").read_text())
    require(manifest["schema_version"] == 1 and manifest["trials"] == 192 and
            manifest["repetitions"] == 2 and manifest["profiles"] == model.PROFILES and
            manifest["modes"] == list(MODES), "Unexpected E4 manifest")
    require([(run["mode"], run["trials"]) for run in manifest["runs"]] == list(TRIALS.items()),
            "Incorrect formal run selection")
    expected = {"data/retries/" + name for name in (*FIXTURES, "selection.json", *GENERATED)}
    for mode in MODES:
        expected.update("data/retries/" + mode + "/" + name for name in
                        ("observations.jsonl.gz", "rpc.jsonl.gz", "driver-logs.jsonl.gz", "run.json",
                         "source-artifact-hashes.json", "recorded-analysis.json", "recorded-independent.json"))
    expected.add("data/retries/bmv2/target-processes.json")
    require(set(manifest["files"]) == expected, "Incorrect E4 file inventory")
    require({str(path.relative_to(root)) for path in archive.rglob("*") if path.is_file()} ==
            expected | {"data/retries/manifest.json"}, "Unexpected E4 archive file")
    for name, item in manifest["files"].items():
        path = root / name
        require(path.resolve().is_relative_to(archive.resolve()), "File outside E4 archive")
        data = path.read_bytes()
        require(len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                f"Size or SHA-256 differs: {name}")
        if name.endswith(".gz"):
            raw = gzip.decompress(data)
            require(len(raw) == item["uncompressed_bytes"] and
                    hashlib.sha256(raw).hexdigest() == item["uncompressed_sha256"] and
                    len(raw.splitlines()) == item["records"], f"Uncompressed record differs: {name}")
            if name.endswith(("observations.jsonl.gz", "rpc.jsonl.gz")):
                require(item["source_sha256"] == item["uncompressed_sha256"], "Original record bytes differ")
    provenance = json.loads((root / "config/retry-source-provenance.json").read_text())
    for name, item in provenance["files"].items():
        require(digest(root / name) == item["export_sha256"], f"Analysis source changed: {name}")
        if item["unchanged"]:
            require(item["source_sha256"] == item["export_sha256"], f"Source correspondence differs: {name}")
    return manifest


def verify_coverage(rows, records, run, plan, corpus):
    mode = run["mode"]
    cases = corpus[mode]
    require(run["completed"] and not run["smoke"] and not run["race"] and
            run["plan"] == plan and run["profiles"] == model.PROFILES and run["repetitions"] == 2 and
            run["case_ids"] == [case["id"] for case in cases], "Incorrect formal run settings")
    require(len(cases) == plan[mode + "_cases"], "Incorrect case corpus")
    jobs = list(itertools.product(range(1, 3), model.PROFILES, cases))
    require(len(rows) == run["observations"] == TRIALS[mode] == len(jobs), "Missing trials")
    require([record["sequence"] for record in records] == list(range(len(records))) and
            all(record["kind"] in ("write", "read") for record in records), "Missing or reordered RPC event")
    covered = []
    for row, (repetition, profile, case) in zip(rows, jobs):
        token = f"{repetition}|{profile}|{case['id']}"
        require((row["repetition"], row["profile"], row["case_id"], row["mode"], row["token"], row["case"]) ==
                (repetition, profile, case["id"], mode, token, case), "Trial identity or order differs")
        require([op["original_index"] for op in case["updates"]] == list(range(len(case["updates"]))) and
                len({op["key"] for op in case["updates"]}) == len(case["updates"]), "Invalid original operation indexes")
        require(1 <= len(row["steps"]) <= plan["max_attempts"] and
                [step["attempt"] for step in row["steps"]] == list(range(1, len(row["steps"]) + 1)),
                "Invalid retry attempt sequence")
        events = [record for record in records if record["token"] == token]
        require(row["rpc_sequences"] == [record["sequence"] for record in events], "Trial RPC sequence differs")
        covered.extend(row["rpc_sequences"])
    require(covered == list(range(len(records))), "Missing or unassigned RPC event")


def verify_driver_logs(root, mode, rows, source_hashes):
    logs = archived(root, mode, "driver-logs")
    expected = {row["token"].replace("|", "__") + "/driver." + suffix
                for row in rows for suffix in ("jsonl", "stderr")}
    require(len(logs) == len(expected) and {log["file"] for log in logs} == expected, "Driver log coverage differs")
    indexed = {}
    for log in logs:
        require(hashlib.sha256(log["text"].encode()).hexdigest() == log["sha256"] == source_hashes[log["file"]],
                "Original driver log hash differs")
        require("DATA RACE" not in log["text"], "Go race report in driver log")
        indexed[log["file"]] = log["text"]
    for row in rows:
        lines = json_lines(indexed[row["token"].replace("|", "__") + "/driver.jsonl"])
        require(lines[0] == dict(profile=row["profile"], ready=True), "Driver readiness differs")
        expected = []
        for step in row["steps"]:
            if not step["result"].get("watchdog_timeout"):
                expected.append(step["result"])
            if "application_read" in step:
                expected.append(step["application_read"])
        require(lines[1:] == expected, "Driver output differs from application observations")


def verify_messages(root, rows, records):
    info = text_format.Parse((root / "data/retries/fixtures/keys.p4info.txt").read_text(), p4i.P4Info())
    table = next(table for table in info.tables if table.preamble.name == "MyIngress.t_exact")
    action = next(action for action in info.actions if action.preamble.name == "MyIngress.forward")
    writes = {}
    for record in records:
        cls = p4r.WriteRequest if record["kind"] == "write" else p4r.ReadRequest
        request = cls.FromString(base64.b64decode(record["request_binary"], validate=True))
        require(json_format.MessageToDict(request, preserving_proto_field_name=True) == record["request"],
                "Binary request differs from JSON")
        if record["kind"] == "write":
            writes[record["sequence"]] = record
            for update in request.updates:
                entry = update.entity.table_entry
                require(entry.table_id == table.preamble.id and len(entry.match) == 1 and
                        entry.match[0].field_id == table.match_fields[0].id and
                        entry.match[0].WhichOneof("field_match_type") == "exact" and
                        0 < int.from_bytes(entry.match[0].exact.value, "big") < 1 << table.match_fields[0].bitwidth,
                        "Write table or match differs from P4Info")
                if update.type != p4r.Update.DELETE:
                    value = entry.action.action
                    require(value.action_id == action.preamble.id and len(value.params) == 1 and
                            value.params[0].param_id == action.params[0].id and
                            0 < int.from_bytes(value.params[0].value, "big") < 1 << action.params[0].bitwidth,
                            "Write action differs from P4Info")
        else:
            require(record["rpc_code"] == 0, "Application Read failed")
            responses = [json_format.MessageToDict(p4r.ReadResponse.FromString(base64.b64decode(raw, validate=True)),
                                                 preserving_proto_field_name=True) for raw in record["response_binary"]]
            require(responses == record["responses"], "Binary Read response differs from JSON")
    for row in rows:
        events = [writes[index] for index in row["rpc_sequences"] if index in writes]
        blocked = []
        for step, record in zip(row["steps"], events):
            result = step["result"]
            require(result["token"] == row["token"], "Write result token differs")
            if result.get("watchdog_timeout"):
                require(result["watchdog_seconds"] == 6, "Watchdog duration differs")
                continue
            if "raw_status" in record:
                require(result.get("raw_status") == record["raw_status"], "Captured RPC status differs")
            elif "raw_status" in result:
                status = rpc_status.Status.FromString(base64.b64decode(result["raw_status"], validate=True))
                require(status.code == record["rpc_code"] and not status.details,
                        "Synthesized gRPC status differs from the RPC-wide error")
            codes, origin = policy.complete_codes(result, len(step["pending"]), row["profile"])
            require(step["codes"] == codes and step["status_source"] == origin, "Public status interpretation differs")
            decision = model.retry_plan(step["pending"], codes)
            if decision.get("read_required"):
                require("application_read" in step, "Required reconciliation Read is missing")
                read = step["application_read"]
                require(read["rpc_code"] == 0 and read["token"] == row["token"], "Reconciliation Read failed")
                decision = model.retry_plan(step["pending"], codes, model.state_map(read["entities"]))
            require(step["decision"] == decision, "Application retry decision differs")
            blocked.extend(decision["blocked"])
        require(row["blocked"] == blocked, "Permanent-error indexes differ")


def prepare_run(root, output, selected, plan, corpus):
    mode = selected["mode"]
    source = root / "data/retries" / mode
    run = json.loads((source / "run.json").read_text())
    require(run["mode"] == mode and digest(source / "run.json") == selected["source_manifest_sha256"],
            "Original run metadata differs")
    original = json.loads((source / "source-artifact-hashes.json").read_text())
    require(original["manifest.json"] == digest(source / "run.json"), "Original run hash differs")
    folder = output / mode
    folder.mkdir()
    for name in ("observations", "rpc"):
        raw = gzip.decompress((source / (name + ".jsonl.gz")).read_bytes())
        require(hashlib.sha256(raw).hexdigest() == original[name + ".jsonl"], "Original trace hash differs")
        (folder / (name + ".jsonl")).write_bytes(raw)
    rows = json_lines((folder / "observations.jsonl").read_bytes())
    records = json_lines((folder / "rpc.jsonl").read_bytes())
    verify_coverage(rows, records, run, plan, corpus)
    verify_driver_logs(root, mode, rows, original)
    verify_messages(root, rows, records)
    provenance = json.loads((root / "config/retry-source-provenance.json").read_text())
    for item in provenance["files"].values():
        name = "experiments/batch-retry/" + item["source_file"].split("/source/experiments/batch-retry/")[1]
        require(run["source_sha256"][name] == item["source_sha256"], "Analysis differs from execution snapshot")
    hashes = {}
    for exported, original_name in FIXTURES.items():
        fixture = root / "data/retries" / exported
        require(run["source_sha256"][original_name] == digest(fixture), "Fixture differs from execution snapshot")
        path = folder / "source" / original_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fixture.read_bytes())
        hashes[original_name] = digest(path)
    run["source_sha256"] = hashes
    (folder / "manifest.json").write_text(json.dumps(run, indent=2) + "\n")
    if mode == "bmv2":
        path = source / "target-processes.json"
        require(digest(path) == original[path.name], "Target process history differs")
        history = json.loads(path.read_text())
        require(len(history) == 1 and all(p["stop_ns"] > p["start_ns"] and "exit_code" in p for p in history),
                "Target stop record missing")
        (folder / path.name).write_bytes(path.read_bytes())
    inventory = {str(path.relative_to(folder)): digest(path) for path in folder.rglob("*") if path.is_file()}
    (folder / "artifact-hashes.json").write_text(json.dumps(inventory, indent=2) + "\n")
    return folder, rows


def summaries(output, runs):
    counts, profiles, trials = [], [], []
    for mode, result, rows in runs:
        counts.append(dict(mode=mode, **{key: result["counts"].get(key, 0) for key in
                      ("trials", "completed", "blocked", "watchdog_timeout", "write_requests",
                       "update_attempts", "successful_updates", "failed_updates", "application_reads")}))
        for profile in model.PROFILES:
            if not any(row["profile"] == profile for row in rows):
                continue
            profiles.append(dict(mode=mode, profile=profile, **{outcome: result["profile_outcomes"].get(profile + ":" + outcome, 0)
                            for outcome in ("completed", "blocked", "watchdog_timeout")}))
        for row, checked in zip(rows, result["trials"]):
            trials.append(dict(mode=mode, repetition=row["repetition"], profile=row["profile"], case_id=row["case_id"],
                               outcome=checked["outcome"], writes=checked["writes"], application_reads=checked["reads"],
                               update_attempts=sum(checked["attempts"].values()),
                               final_state_matches_desired=str(checked["final_state_matches_desired"]).lower()))
    for name, records in zip(GENERATED, (counts, profiles, trials)):
        with (output / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)


def verify(root, output):
    require(__debug__, "Run without -O so the evidence checks remain enabled")
    manifest = verify_inventory(root)
    require(not output.resolve().is_relative_to((root / "data").resolve()), "Output must be outside archived data")
    output.mkdir(parents=True, exist_ok=False)
    plan = json.loads((root / "data/retries/plan.json").read_text())
    corpus = json.loads((root / "data/retries/fixtures/cases.json").read_text())
    require(plan["cases_sha256"] == digest(root / "data/retries/fixtures/cases.json"), "Locked cases differ")
    selection = json.loads((root / "data/retries/selection.json").read_text())
    runs = []
    for selected in manifest["runs"]:
        mode = selected["mode"]
        require(selection[mode] == selected["source_directory"], "Selected source directory differs")
        folder, rows = prepare_run(root, output, selected, plan, corpus)
        primary = analyze.analyze(folder)
        command = [sys.executable, "-I", str(root / "scripts/retry_checks/secondary.py"), str(folder)]
        independent = subprocess.run(command, capture_output=True, text=True)
        (folder / "secondary.log").write_text(independent.stdout + independent.stderr)
        require(independent.returncode == 0, f"Independent verification failed: {mode}. See {folder / 'secondary.log'}")
        for name, recorded in (("analysis.json", "recorded-analysis.json"),
                               ("independent-verification.json", "recorded-independent.json")):
            require((folder / name).read_bytes() == (root / "data/retries" / mode / recorded).read_bytes(),
                    f"Recomputed report differs: {mode}/{name}")
        runs.append((mode, primary, rows))
    summaries(output, runs)
    for name in GENERATED:
        require((output / name).read_bytes() == (root / "data/retries" / name).read_bytes(), f"Recomputed summary differs: {name}")
    counts = {key: sum(result["counts"].get(key, 0) for _, result, _ in runs) for key in runs[0][1]["counts"]}
    require(counts == dict(write_requests=334, update_attempts=882, successful_updates=620, failed_updates=262,
                           trials=192, completed=166, blocked=10, application_reads=112, watchdog_timeout=16),
            "Incorrect E4 totals")
    record = dict(verified_local=datetime.now().astimezone().isoformat(), counts=counts,
                  manifest_sha256=digest(root / "data/retries/manifest.json"),
                  analysis_provenance_sha256=digest(root / "config/retry-source-provenance.json"))
    (output / "verification.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/retries")
    args = parser.parse_args()
    result = verify(ROOT, args.output.resolve())
    print(json.dumps(dict(verified=True, **result["counts"])))


if __name__ == "__main__":
    main()
