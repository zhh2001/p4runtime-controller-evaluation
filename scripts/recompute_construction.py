"""Verify construction outputs and recompute the saved round summaries."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

import numpy as np

from recompute_rpc import read_csv, require


ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ("kind", "profile", "workload", "mode", "component")
METRICS = ("ns_per_op", "bytes_per_op", "allocs_per_op")
SUMMARY_FIELDS = ("mean", "median", "sample_stddev", "minimum", "maximum",
                  "mean_ci95_low", "mean_ci95_high")
PROFILES = ("sdk", "raw-current", "legacy", "raw-legacy", "finsy", "shell")
COMPONENTS = ("table_lookup/exact", "table_lookup/mixed", "action_lookup/exact",
              "action_lookup/mixed", "clone_entry/exact", "clone_entry/mixed", "encode_exact32")


def identity(row):
    return tuple(row[field] for field in IDENTITY)


def conditions(plan):
    require(plan["profiles"] == list(PROFILES) and plan["workloads"] == ["exact", "mixed"],
            "Unexpected construction profiles or workloads")
    require(plan["components_sdk_only"] == list(COMPONENTS), "Unexpected component conditions")
    result = []
    for profile in PROFILES:
        modes = ["new"] if profile.startswith("raw-") else ["new", "reused"]
        require(plan["modes"][profile] == modes, "Unexpected construction modes")
        result.extend(dict(kind="entry", profile=profile, workload=workload, mode=mode, component="")
                      for workload in plan["workloads"] for mode in modes)
    for item in COMPONENTS:
        component, _, workload = item.partition("/")
        result.append(dict(kind="component", profile="sdk", workload=workload or "exact",
                           mode="", component=component))
    return result


def verify_files(root, manifest):
    expected = {"data/construction/" + name for name in (
        "plan.json", "run.json", "environment.json", "preflight.json", "records.jsonl",
        "measurement-rounds.csv", "round-statistics.csv", "paired-ratios.csv",
        "fixtures/inputs.json", "fixtures/construct.p4info.txt")}
    expected.update(f"data/construction/stdout/{number:03}.txt" for number in range(1, 271))
    expected.update("figures/construction/construction-time." + suffix for suffix in ("pdf", "png"))
    require(manifest["schema_version"] == 1 and set(manifest["files"]) == expected,
            "Incorrect construction file inventory")
    for name, record in manifest["files"].items():
        data = (root / name).read_bytes()
        require(len(data) == record["bytes"] and hashlib.sha256(data).hexdigest() == record["sha256"],
                f"Size or SHA-256 differs: {name}")
        if name.endswith(".csv"):
            require(len(read_csv(root / name)) == record["rows"], f"CSV row count differs: {name}")


def load_rounds(path, selected_conditions, repetitions, schedule):
    rows = read_csv(path)
    expected = {(*identity(condition), repetition)
                for condition in selected_conditions for repetition in range(1, repetitions + 1)}
    index = {}
    for number, row in enumerate(rows, 1):
        key = (*identity(row), int(row["repetition"]))
        require(key not in index, f"Duplicate measurement: {key}")
        require(key in expected, f"Unexpected condition: {key}")
        require(int(row["sequence"]) == number and number <= len(schedule) and
                key == (*identity(schedule[number - 1]), schedule[number - 1]["repetition"]),
                f"Measurement order differs: {key}")
        for field in ("iterations", "elapsed_ns"):
            require(int(row[field]) > 0, f"Invalid measurement {field}: {key}")
        value = float(row["ns_per_op"])
        require(math.isfinite(value) and value > 0 and
                math.isclose(value, int(row["elapsed_ns"]) / int(row["iterations"]), rel_tol=1e-12),
                f"Invalid measurement ns_per_op: {key}")
        for field in METRICS[1:]:
            if row["profile"] in ("finsy", "shell"):
                require(row[field] == "", f"Python allocation metric must be empty: {key}")
            else:
                value = float(row[field])
                require(math.isfinite(value) and value >= 0 and value.is_integer(),
                        f"Invalid allocation metric: {key}")
        index[key] = row
    require(set(index) == expected, "Incomplete measurement coverage")
    return rows, index


def parse_output(text, scheduled):
    if scheduled["profile"] in ("finsy", "shell"):
        result = json.loads(text)
        require(result["gc_enabled"] is True, "Python GC was not enabled")
        require(all(result[field] == scheduled[field] for field in ("profile", "workload", "mode")),
                "Python benchmark identity differs")
    else:
        lines = [line.split() for line in text.splitlines() if line.startswith("Benchmark")]
        require(len(lines) == 1 and "PASS" in text.splitlines(), "Incomplete Go benchmark output")
        fields = lines[0]
        if scheduled["kind"] == "entry":
            name = "/".join(["BenchmarkConstruct", *[scheduled[k] for k in ("profile", "workload", "mode")]])
        else:
            name = "BenchmarkComponents/" + scheduled["component"]
            if scheduled["component"] != "encode_exact32":
                name += "/" + scheduled["workload"]
        require(fields[0] == name and len(fields[2:]) % 2 == 0, "Go benchmark identity differs")
        units = {fields[i + 1]: float(fields[i]) for i in range(2, len(fields), 2)}
        require(set(units) == {"ns/op", "elapsed-ns", "B/op", "allocs/op"} and
                all(math.isfinite(value) and value >= 0 for value in units.values()) and
                units["elapsed-ns"].is_integer(), "Invalid Go benchmark metrics")
        iterations = int(fields[1])
        require(iterations > 0, "Invalid benchmark iteration count")
        result = dict(iterations=iterations, elapsed_ns=int(units["elapsed-ns"]),
                      ns_per_op=units["elapsed-ns"] / iterations, reported_ns_per_op=units["ns/op"],
                      bytes_per_op=units["B/op"], allocs_per_op=units["allocs/op"],
                      timing_basis="WSL CLOCK_MONOTONIC")
        require(math.isclose(result["ns_per_op"], units["ns/op"], rel_tol=5e-4),
                "Rounded Go ns/op differs from the elapsed-time ratio")
    require(type(result["iterations"]) is int and type(result["elapsed_ns"]) is int and
            result["iterations"] > 0 and result["elapsed_ns"] > 0 and
            math.isfinite(result["ns_per_op"]) and
            math.isclose(result["ns_per_op"], result["elapsed_ns"] / result["iterations"], rel_tol=1e-12),
            "Invalid elapsed time or iteration count")
    return result


def verify_outputs(base, rows, run, environment):
    records = [json.loads(line) for line in (base / "records.jsonl").read_text().splitlines()]
    require(len(records) == len(rows) == 270, "Incomplete output records")
    for number, (record, saved) in enumerate(zip(records, rows), 1):
        require(identity(record) == identity(saved) and record["repetition"] == int(saved["repetition"]) and
                record["sequence"] == number and record["exit_code"] == record["stderr_bytes"] == 0 and
                record["cpu"] == run["affinity_cpu"], "Output record differs from the measurement")
        parsed = parse_output((base / f"stdout/{number:03}.txt").read_text(), saved)
        require(parsed == record["metrics"], "Output metrics differ from the saved record")
        require(record["process_duration_ns"] >= parsed["elapsed_ns"], "Invalid process duration")
        if record["profile"] in ("finsy", "shell"):
            require(parsed["python"] == environment["python"][record["profile"]]["python"],
                    "Python version differs")
        for field in ("iterations", "elapsed_ns", *METRICS):
            if saved[field] != "":
                require(float(saved[field]) == parsed[field], f"Output and CSV differ: {field}")


def quantile(ordered, probability):
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(values, indices):
    require(len(values) >= 2 and all(math.isfinite(value) for value in values), "Invalid sample")
    means = sorted(math.fsum(values[i] for i in sample) / len(values) for sample in indices)
    return dict(rounds=len(values), mean=statistics.mean(values), median=statistics.median(values),
                sample_stddev=statistics.stdev(values), minimum=min(values), maximum=max(values),
                mean_ci95_low=quantile(means, .025), mean_ci95_high=quantile(means, .975))


def compare_summary(actual, saved):
    require(int(saved["rounds"]) == actual["rounds"], "Summary round count differs")
    for field in SUMMARY_FIELDS:
        require(math.isfinite(float(saved[field])) and
                math.isclose(float(saved[field]), actual[field], rel_tol=1e-9, abs_tol=1e-8),
                f"Summary statistic differs: {field}")
    return len(SUMMARY_FIELDS)


def pair_definitions():
    result = []
    for workload in ("exact", "mixed"):
        pairs = [(profile, mode, raw, "new")
                 for profile, raw in (("sdk", "raw-current"), ("legacy", "raw-legacy"))
                 for mode in ("new", "reused")]
        pairs.extend((profile, "reused", profile, "new") for profile in ("sdk", "legacy", "finsy", "shell"))
        result.extend((workload, *pair) for pair in pairs)
    return result


def paired_values(index, pair, repetitions):
    workload, numerator, mode, denominator, baseline = pair
    return [float(index["entry", numerator, workload, mode, "", rep]["ns_per_op"]) /
            float(index["entry", denominator, workload, baseline, "", rep]["ns_per_op"])
            for rep in range(1, repetitions + 1)]


def recompute(root, output):
    base = root / "data/construction"
    manifest = json.loads((base / "manifest.json").read_text())
    verify_files(root, manifest)
    plan = json.loads((base / "plan.json").read_text())
    run = json.loads((base / "run.json").read_text())
    selected_conditions = conditions(plan)
    require(selected_conditions == manifest["conditions"] and plan["repetitions"] == manifest["repetitions"] == 10,
            "Construction conditions differ")
    require(run["completed"] and not run["smoke"] and not run["failures"] and
            run["completed_measurements"] == 270, "Incomplete formal run")
    rng = random.Random(plan["seed"])
    schedule = []
    for repetition in range(1, 11):
        block = list(selected_conditions)
        rng.shuffle(block)
        schedule.extend(dict(**condition, repetition=repetition) for condition in block)
    require(run["schedule"] == schedule, "Randomized schedule differs")
    rows, index = load_rounds(base / "measurement-rounds.csv", selected_conditions, 10, schedule)
    environment = json.loads((base / "environment.json").read_text())
    verify_outputs(base, rows, run, environment)
    preflight = json.loads((base / "preflight.json").read_text())
    require(preflight["total_semantic_samples"] == sum(preflight["semantic_samples"].values()) == 5120 and
            preflight["ownership_checks"] and preflight["go_race_and_vet_passed"], "Invalid recorded preflight")
    bootstrap = manifest["bootstrap"]
    require(bootstrap["seed"] == plan["seed"] == 20261019 and bootstrap["resamples"] == 20000 and
            bootstrap["rng"] == "NumPy PCG64" and bootstrap["quantile"] == "linear", "Bootstrap method differs")
    indices = np.random.Generator(np.random.PCG64(bootstrap["seed"])).integers(0, 10, (20000, 10)).tolist()
    saved_metrics = read_csv(base / "round-statistics.csv")
    expected = {(*identity(condition), metric) for condition in selected_conditions
                for metric in (METRICS[:1] if condition["profile"] in ("finsy", "shell") else METRICS)}
    actual = [(*identity(row), row["metric"]) for row in saved_metrics]
    require(len(actual) == len(set(actual)) == 65 and set(actual) == expected, "Incomplete metric summaries")
    summaries, pairs = [], []
    checked = 0
    for saved in saved_metrics:
        values = [float(index[(*identity(saved), rep)][saved["metric"]]) for rep in range(1, 11)]
        result = summarize(values, indices)
        checked += compare_summary(result, saved)
        summaries.append({**{field: saved[field] for field in (*IDENTITY, "metric")}, **result})
    saved_pairs = read_csv(base / "paired-ratios.csv")
    fields = ("workload", "numerator", "numerator_mode", "denominator", "denominator_mode")
    keys = [tuple(row[field] for field in fields) for row in saved_pairs]
    require(len(keys) == len(set(keys)) == 16 and set(keys) == set(pair_definitions()), "Incomplete paired summaries")
    for saved, key in zip(saved_pairs, keys):
        require(saved["metric"] == "construction_time_ratio", "Unexpected paired metric")
        result = summarize(paired_values(index, key, 10), indices)
        checked += compare_summary(result, saved)
        pairs.append({**{field: saved[field] for field in (*fields, "metric")}, **result})
    report = dict(passed=True, conditions=len(selected_conditions), measurement_rows=len(rows),
                  output_measurements_verified=len(rows), go_outputs=190, python_outputs=80,
                  metric_summaries=len(summaries), paired_summaries=len(pairs), numeric_fields_checked=checked,
                  bootstrap=bootstrap, numpy_version=np.__version__,
                  statistics_reference="math.fsum, statistics and sorted linear-interpolation quantiles",
                  recorded_preflight_samples=preflight["total_semantic_samples"],
                  manifest_sha256=hashlib.sha256((base / "manifest.json").read_bytes()).hexdigest(),
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  files_sha256={name: record["sha256"] for name, record in manifest["files"].items()})
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("round-statistics.csv", summaries), ("paired-ratios.csv", pairs)):
        with (output / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/construction")
    args = parser.parse_args()
    require(not any(args.output.resolve().is_relative_to(ROOT / name) for name in ("data", "figures")),
            "Output must be outside the archived data and figures")
    report = recompute(ROOT, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "files_sha256"}, indent=2))


if __name__ == "__main__":
    main()
