"""Verify the archived E1 inputs, requests, outcomes and target state."""

import argparse
from collections import Counter
from datetime import datetime
import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil

from finsy.proto import p4i
from google.protobuf import text_format

from input_checks import baseline
from input_checks.boundaries import analyze as boundary_analysis, secondary as boundary_secondary
from input_checks.constraints import analyze as constraint_analysis, secondary as constraint_secondary


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("sdk", "raw-current", "legacy", "raw-legacy", "finsy", "shell")
RUNS = ("baseline/controlled", "baseline/bmv2", "boundaries/controlled", "boundaries/bmv2",
        "constraints/controlled", "constraints/bmv2")
IDENTITY = ("profile", "repetition", "case_id")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def write_csv(path, rows):
    with path.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def verify_inventory(root):
    manifest = json.loads((root / "data/inputs/manifest.json").read_text())
    require(manifest["schema_version"] == 1 and manifest["observations"] == 6984 and
            manifest["profiles"] == list(PROFILES) and manifest["repetitions"] == 2,
            "Unexpected E1 manifest")
    require([run["id"] for run in manifest["runs"]] == list(RUNS), "Incorrect E1 run selection")
    expected_files = set()
    for run_id in RUNS:
        expected_files.update("data/inputs/" + run_id + "/" + name
                              for name in ("observations.jsonl.gz", "rpc.jsonl.gz", "run.json"))
        group, mode = run_id.split("/")
        names = ("recorded-counts.csv",) if group == "baseline" else ("case-results.csv", "counts.csv")
        expected_files.update("data/inputs/" + run_id + "/" + name for name in names)
        if group == "constraints":
            expected_files.add("data/inputs/" + run_id + "/findings.json")
    expected_files.add("data/inputs/baseline/bmv2/readback-evaluation.jsonl.gz")
    fixtures = {"baseline": ("cases.json", "controlled.p4info.txt", "bmv2.p4info.txt", "tables.p4"),
                "boundaries": ("cases.json", "controlled.p4info.txt", "target.p4info.txt", "boundaries.p4"),
                "constraints": ("cases.json", "target.p4info.txt", "schema.json", "tables.p4")}
    for group, names in fixtures.items():
        expected_files.update("data/inputs/" + group + "/fixtures/" + name for name in names)
        if group != "baseline":
            expected_files.add("data/inputs/" + group + "/plan.json")
    require(set(manifest["files"]) == expected_files, "Incorrect E1 file inventory")
    for name, item in manifest["files"].items():
        path = root / name
        require(path.resolve().is_relative_to((root / "data/inputs").resolve()), "File outside E1 data")
        data = path.read_bytes()
        require(len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                f"Size or SHA-256 differs: {name}")
        if name.endswith(".gz"):
            raw = gzip.decompress(data)
            require(len(raw) == item["uncompressed_bytes"] and
                    hashlib.sha256(raw).hexdigest() == item["uncompressed_sha256"] and
                    len(raw.splitlines()) == item["records"], f"Uncompressed record differs: {name}")
    provenance = json.loads((root / "config/input-source-provenance.json").read_text())
    for name, item in provenance["files"].items():
        require(digest(root / name) == item["export_sha256"], f"Analysis source changed: {name}")
        if item["unchanged"]:
            require(item["source_sha256"] == item["export_sha256"], f"Source correspondence differs: {name}")
    return manifest


def verify_coverage(observations, events, corpus, run):
    cases = {case["id"]: case for case in corpus}
    require(len(cases) == len(corpus), "Duplicate corpus input")
    ids = run["case_ids"]
    require(len(set(ids)) == len(ids) and set(ids) <= cases.keys(), "Incorrect run case selection")
    expected = {(profile, repetition, case_id)
                for profile in PROFILES for repetition in (1, 2) for case_id in ids}
    actual = []
    for row in observations:
        profile = row.get("profile", row.get("adapter"))
        case_id = row.get("id", row.get("case_id"))
        actual.append((profile, row["repetition"], case_id))
        if run["group"] == "baseline":
            require(row["category"] == cases[case_id]["category"] and row["suite"] == "E1", "Baseline input differs")
        else:
            require(row["case"] == cases[case_id] and row["mode"] == run["mode"], "Embedded source input differs")
            require(row["stage"] in ("completed", "not_expressible"), "Unexpected driver stage")
            require(row["token"] == f"{row['repetition']}|{profile}|{case_id}", "Observation token differs")
    require(len(actual) == len(set(actual)) == len(expected) == run["observations"] and set(actual) == expected,
            "Missing, duplicate or unexpected E1 observation")
    if run["group"] != "baseline":
        require([event["sequence"] for event in events] == list(range(len(events))), "RPC sequence differs")
        tokens = {row["token"] for row in observations}
        require(len({event["token"] for event in events}) == len(events) and
                all(event["token"] in tokens for event in events), "Unmatched or duplicate RPC event")
        for event in events:
            request = event["request"]
            require(int(request["device_id"]) == 1, "RPC device ID differs")
            if event["operation"] == "write":
                require(int(request["election_id"]["low"]) == 1, "RPC election ID differs")


def prepare_run(root, output, run):
    group, mode = run["group"], run["mode"]
    source = root / "data/inputs" / group / mode
    folder = output / group / mode
    folder.mkdir(parents=True)
    metadata = json.loads((source / "run.json").read_text())
    require(metadata["source_manifest_sha256"] == run["source_manifest_sha256"] and
            metadata["observations"] == run["observations"] and len(metadata["case_ids"]) == run["cases"] and
            metadata["group"] == group and metadata["mode"] == mode and metadata["profiles"] == list(PROFILES) and
            metadata["repetitions"] == 2,
            "Run metadata differs from the catalog")
    for name in ("observations", "rpc"):
        (folder / (name + ".jsonl")).write_bytes(gzip.decompress((source / (name + ".jsonl.gz")).read_bytes()))
    corpus = json.loads((root / "data/inputs" / group / "fixtures/cases.json").read_text())
    observations, events = json_lines(folder / "observations.jsonl"), json_lines(folder / "rpc.jsonl")
    verify_coverage(observations, events, corpus, metadata)
    original = "table-constraints" if group == "constraints" else group
    hashes = {}
    for path in sorted((root / "data/inputs" / group / "fixtures").iterdir()):
        relative = "experiments/" + original + "/fixtures/" + path.name
        target = folder / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[relative] = digest(target)
    metadata.update(completed=True, source_sha256=hashes)
    (folder / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return folder, metadata, corpus, observations, events


def verify_baseline(root, folder, run, corpus, observations, events):
    fixture = root / "data/inputs/baseline/fixtures" / (run["mode"] + ".p4info.txt")
    info = text_format.Parse(fixture.read_text(), p4i.P4Info())
    evaluations, _, result = baseline.verify(info, corpus, observations, events, run["mode"])
    baseline.check_recorded_counts(root / "data/inputs/baseline" / run["mode"] / "recorded-counts.csv", observations, run["mode"])
    if run["mode"] == "bmv2":
        saved = [json.loads(line) for line in gzip.decompress(
            (root / "data/inputs/baseline/bmv2/readback-evaluation.jsonl.gz").read_bytes()).splitlines()]
        require(len(saved) == len(observations), "Incomplete saved readback evaluations")
        for row, recorded in zip(observations, saved):
            changes = baseline.normalize_read(row["after"][0], info)[1] if row.get("call_succeeded") else []
            expected = dict(adapter=row["adapter"], suite="E1", case_id=row["case_id"], repetition=row["repetition"],
                            stage=row["stage"], category=row["category"], rpc_code=row.get("rpc_code"),
                            state_count=len(row["after"]), normalizations=changes)
            if row.get("call_succeeded"):
                expected["readback_semantics_match"] = True
            require(recorded == expected, "Saved readback evaluation differs")
    write_csv(folder / "case-results.csv", evaluations)
    return evaluations, result


def verify_run(root, output, selected):
    folder, run, corpus, observations, events = prepare_run(root, output, selected)
    if run["group"] == "baseline":
        evaluations, result = verify_baseline(root, folder, run, corpus, observations, events)
    else:
        analysis = boundary_analysis if run["group"] == "boundaries" else constraint_analysis
        secondary = boundary_secondary if run["group"] == "boundaries" else constraint_secondary
        analysis.analyze(folder)
        for name in ("case-results.csv", "counts.csv"):
            require((folder / name).read_bytes() == (root / "data/inputs" / selected["id"] / name).read_bytes(),
                    f"Recomputed CSV differs: {selected['id']}/{name}")
        if run["group"] == "constraints":
            require((folder / "findings.json").read_bytes() ==
                    (root / "data/inputs" / selected["id"] / "findings.json").read_bytes(), "Findings differ")
        result = json.loads((folder / "verification.json").read_text())
        result.pop("files_sha256")
        result["secondary"] = secondary.verify(folder)
        evaluations = read_csv(folder / "case-results.csv")
    result.update(id=selected["id"], cases=len(run["case_ids"]), observations=len(observations), rpc_calls=len(events))
    return evaluations, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/inputs")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so the semantic checks remain enabled.")
    output = args.output.resolve()
    if output.is_relative_to(ROOT / "data") or output.is_relative_to(ROOT / "figures"):
        parser.error("Output must be outside the archived data and figures.")
    manifest = verify_inventory(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    all_rows, results = [], []
    fields = ("profile", "repetition", "case_id", "family", "category", "outcome", "rpc_observed", "rpc_code",
              "semantics_match", "state_matches_request", "flags")
    for run in manifest["runs"]:
        evaluations, result = verify_run(ROOT, output, run)
        results.append(result)
        all_rows.extend(dict(group=run["group"], mode=run["mode"], **{field: row.get(field, "") for field in fields})
                        for row in evaluations)
    write_csv(output / "case-results.csv", all_rows)
    keys = ("group", "mode", "profile", "category", "outcome")
    per_round = [Counter(tuple(str(row[k]) for k in keys) for row in all_rows if int(row["repetition"]) == repetition)
                 for repetition in (1, 2)]
    require(per_round[0] == per_round[1], "Repetition classifications differ")
    write_csv(output / "counts.csv", [dict(zip(keys, key), count_per_round=count) for key, count in sorted(per_round[0].items())])
    record = dict(verified_local=datetime.now().astimezone().isoformat(), observations=len(all_rows),
                  cases=382, runs=results, repeated_classifications_identical=True,
                  manifest_sha256=digest(ROOT / "data/inputs/manifest.json"),
                  analysis_provenance_sha256=digest(ROOT / "config/input-source-provenance.json"),
                  files_sha256={str(p.relative_to(output)): digest(p) for p in sorted(output.rglob("*"))
                                if p.is_file() and p != output / "verification.json"})
    require(record["observations"] == 6984, "Incorrect total E1 observations")
    (output / "verification.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"verified": True, "observations": 6984, "cases": 382, "runs": 6, "output": str(output)}))


if __name__ == "__main__":
    main()
