"""Recompute and audit newly recorded local-construction measurements."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "scripts"))
from recompute_construction import parse_output


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_csv(path, rows):
    with path.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def key(row):
    return tuple(row[name] for name in ("kind", "profile", "workload", "mode", "component"))


def recompute(output):
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["completed"] and not manifest["failures"]
    for name, expected in manifest["source_sha256"].items():
        assert digest(output / "source" / name) == expected, name
    raw = [json.loads(line) for line in (output / "measurements.jsonl").read_text().splitlines()]
    assert len(raw) == manifest["completed_measurements"] == len(manifest["schedule"])
    rows = []
    for number, (record, scheduled) in enumerate(zip(raw, manifest["schedule"]), 1):
        assert key(record) == key(scheduled) and record["repetition"] == scheduled["repetition"]
        assert record["sequence"] == number and record["exit_code"] == 0
        assert record["cpu"] == manifest["affinity_cpu"]
        stdout = (output / f"{number:03}-stdout.txt").read_text()
        metrics = record["metrics"]
        if record["profile"] in ("finsy", "shell"):
            original = json.loads(stdout)
            assert original["elapsed_ns"] == metrics["elapsed_ns"]
            assert original["iterations"] == metrics["iterations"] and original["gc_enabled"]
        else:
            fields = next(line.split() for line in stdout.splitlines() if line.startswith("Benchmark"))
            values = {fields[i+1]: float(fields[i]) for i in range(2, len(fields), 2)}
            assert int(fields[1]) == metrics["iterations"]
            assert values["elapsed-ns"] == metrics["elapsed_ns"]
            assert values["B/op"] == metrics["bytes_per_op"] and values["allocs/op"] == metrics["allocs_per_op"]
            assert math.isclose(values["ns/op"], metrics["ns_per_op"], rel_tol=5e-4)
        assert metrics["iterations"] > 0 and metrics["elapsed_ns"] > 0
        assert math.isclose(metrics["ns_per_op"], metrics["elapsed_ns"] / metrics["iterations"], rel_tol=1e-12)
        rows.append(dict(sequence=number, **{k: record[k] for k in
                                           ("kind", "profile", "workload", "mode", "component", "repetition")},
                         iterations=metrics["iterations"], elapsed_ns=metrics["elapsed_ns"],
                         ns_per_op=metrics["elapsed_ns"] / metrics["iterations"],
                         bytes_per_op=metrics.get("bytes_per_op", ""), allocs_per_op=metrics.get("allocs_per_op", "")))
    expected_counts = Counter(key(row) for row in manifest["schedule"])
    assert Counter(key(row) for row in rows) == expected_counts
    assert len(expected_counts) == 27 and set(expected_counts.values()) == {manifest["plan"]["repetitions"]}
    write_csv(output / "measurement-rounds.csv", rows)
    return manifest, rows


def summarize(values, indices):
    values = np.asarray(values, dtype=float)
    bootstrap = values[indices].mean(axis=1)
    low, high = np.quantile(bootstrap, [0.025, 0.975])
    return dict(rounds=len(values), mean=float(values.mean()), median=float(np.median(values)),
                sample_stddev=float(values.std(ddof=1)), minimum=float(values.min()), maximum=float(values.max()),
                mean_ci95_low=float(low), mean_ci95_high=float(high))


def audit_outputs(output):
    manifest = json.loads((output / "manifest.json").read_text())
    records = [json.loads(line) for line in (output / "measurements.jsonl").read_text().splitlines()]
    for number, (record, scheduled) in enumerate(zip(records, manifest["schedule"]), 1):
        assert not (output / f"{number:03}-stderr.txt").read_bytes(), number
        parsed = parse_output((output / f"{number:03}-stdout.txt").read_text(), scheduled)
        assert parsed == record["metrics"], number
        assert record["process_duration_ns"] >= parsed["elapsed_ns"], number


def analyze(output):
    manifest, rows = recompute(output)
    assert not manifest["smoke"] and manifest["plan"]["repetitions"] == 10 and len(rows) == 270
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    indices = np.random.default_rng(manifest["plan"]["seed"]).integers(0, 10, size=(20000, 10))
    statistics_rows = []
    for identity, data in groups.items():
        data.sort(key=lambda row: row["repetition"])
        assert [r["repetition"] for r in data] == list(range(1, 11))
        for metric in ("ns_per_op", "bytes_per_op", "allocs_per_op"):
            if data[0][metric] == "":
                assert all(row[metric] == "" for row in data)
                continue
            statistics_rows.append(dict(**{k: data[0][k] for k in ("kind", "profile", "workload", "mode", "component")},
                                        metric=metric, **summarize([r[metric] for r in data], indices)))
    assert len(statistics_rows) == 65
    index = {(r["profile"], r["workload"], r["mode"], r["repetition"]): r
             for r in rows if r["kind"] == "entry"}
    ratio_rows = []
    for workload in ("exact", "mixed"):
        pairs = [(p, m, raw, "new") for p, raw in (("sdk", "raw-current"), ("legacy", "raw-legacy"))
                 for m in ("new", "reused")]
        pairs += [(p, "reused", p, "new") for p in ("sdk", "legacy", "finsy", "shell")]
        for numerator, mode, denominator, baseline_mode in pairs:
            values = [index[numerator, workload, mode, rep]["ns_per_op"] /
                      index[denominator, workload, baseline_mode, rep]["ns_per_op"] for rep in range(1, 11)]
            ratio_rows.append(dict(workload=workload, numerator=numerator, numerator_mode=mode,
                                   denominator=denominator, denominator_mode=baseline_mode,
                                   metric="construction_time_ratio", **summarize(values, indices)))
    write_csv(output / "round-statistics.csv", statistics_rows)
    write_csv(output / "paired-ratios.csv", ratio_rows)
    audit_outputs(output)
    record_verification(output, manifest, rows, 65, 16)
    print(json.dumps(dict(output=str(output), measurements=len(rows), metric_summaries=65, paired_summaries=16)))


def record_verification(output, manifest, rows, summaries=0, pairs=0):
    analysis_sources = output / "analysis-source"
    analysis_sources.mkdir(exist_ok=True)
    for name in ("analyze.py", "verify_statistics.py"):
        shutil.copyfile(ROOT / name, analysis_sources / name)
    files = [p for p in output.rglob("*")
             if p.is_file() and p.name not in ("verification.json", "summary-crosscheck.json")]
    record = dict(verified_local=datetime.now().astimezone().isoformat(), measurements=len(rows),
                  all_measurements_recomputed=True, metric_summaries=summaries, paired_summaries=pairs,
                  source_snapshot_verified=True, completed_measurements=manifest["completed_measurements"],
                  files_sha256={str(p.relative_to(output)): digest(p) for p in files})
    (output / "verification.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so output checks remain enabled.")
    output = args.output.resolve()
    if args.check_only:
        manifest, rows = recompute(output)
        audit_outputs(output)
        record_verification(output, manifest, rows)
        print(json.dumps(dict(checked=len(rows))))
    else:
        analyze(output)
