"""Check newly executed input-validation runs using the archived analysis logic."""

import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path

from finsy.proto import p4i
from google.protobuf import text_format

from input_checks import baseline
from input_checks.boundaries import analyze as boundary_analysis, secondary as boundary_secondary
from input_checks.constraints import analyze as constraint_analysis, secondary as constraint_secondary
from verify_inputs import json_lines, read_csv, require, write_csv


def verify(folder, group):
    manifest = json.loads((folder / "manifest.json").read_text())
    require(manifest["completed"], "Input run did not complete")
    for name, expected in manifest["source_sha256"].items():
        require(hashlib.sha256((folder / "source" / name).read_bytes()).hexdigest() == expected,
                f"Source snapshot changed: {name}")
    observations = json_lines(folder / "observations.jsonl")
    events = json_lines(folder / "rpc.jsonl")
    expected = {(profile, repetition, case_id) for profile in manifest["profiles"]
                for repetition in range(1, manifest["repetitions"] + 1) for case_id in manifest["case_ids"]}
    identities = [(row.get("profile", row.get("adapter")), row["repetition"], row.get("id", row.get("case_id")))
                  for row in observations]
    require(len(set(identities)) == len(identities) == len(expected) == manifest["observations"] and
            set(identities) == expected, "Missing, duplicate or unexpected input observation")
    original = {"baseline": "minimal", "boundaries": "boundaries", "constraints": "table-constraints"}[group]
    fixtures = folder / "source/experiments" / original / "fixtures"
    corpus = {case["id"]: case for case in json.loads((fixtures / "cases.json").read_text())}
    require(set(manifest["case_ids"]) <= corpus.keys(), "Unknown source case")
    for row in observations:
        case_id = row.get("id", row.get("case_id"))
        if group != "baseline":
            require(row["case"] == corpus[case_id] and row["mode"] == manifest["mode"], "Source input differs")
    for path in folder.rglob("*.stderr"):
        require("DATA RACE" not in path.read_text(), f"Go race report: {path}")
    (folder / "analysis.json").unlink(missing_ok=True)
    if group == "baseline":
        if manifest["mode"] == "bmv2":
            events = [event for event in events if event["event"] == "write_end"]
            require({event["sequence"] for event in events} ==
                    {event["sequence"] for row in observations for event in row["write_events"]}, "Unmatched tested Write")
        info = text_format.Parse((fixtures / ("table_keys.p4info.txt" if manifest["mode"] == "controlled" else "keys.p4info.txt")).read_text(), p4i.P4Info())
        evaluations, _, result = baseline.verify(info, list(corpus.values()), observations, events, manifest["mode"])
        write_csv(folder / "case-results.csv", evaluations)
        counts = Counter((r["profile"], r["category"], r["outcome"]) for r in evaluations)
        write_csv(folder / "counts.csv", [dict(profile=key[0], category=key[1], outcome=key[2], count=value)
                                          for key, value in sorted(counts.items())])
    else:
        analysis = boundary_analysis if group == "boundaries" else constraint_analysis
        secondary = boundary_secondary if group == "boundaries" else constraint_secondary
        analysis.analyze(folder)
        result = json.loads((folder / "verification.json").read_text())
        result.pop("files_sha256")
        result["secondary"] = secondary.verify(folder)
        evaluations = read_csv(folder / "case-results.csv")
    result.update(group=group, mode=manifest["mode"], observations=len(observations), cases=len(manifest["case_ids"]),
                  profiles=manifest["profiles"], repetitions=manifest["repetitions"],
                  verified_local=datetime.now().astimezone().isoformat(),
                  source_snapshot_verified=True,
                  output_sha256={name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                 for name in ("case-results.csv", "counts.csv")})
    (folder / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(group=group, mode=manifest["mode"], observations=len(observations), verified=True)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, choices=("baseline", "boundaries", "constraints"))
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so semantic checks remain enabled.")
    verify(args.run.resolve(), args.group)


if __name__ == "__main__":
    main()
