"""Recompute RPC summaries from the selected per-round measurements."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import random

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_FIELDS = (
    "rounds", "mean", "median", "sample_stddev", "cv_percent",
    "minimum", "maximum", "mean_ci95_low", "mean_ci95_high",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames),
                f"Missing or duplicate CSV columns: {path}")
        rows = list(reader)
    require(rows and all(None not in row and None not in row.values() for row in rows),
            f"Empty or malformed CSV: {path}")
    return rows


def load_rounds(path, point, manifest):
    rows = read_csv(path)
    index = {}
    repetitions = manifest["repetitions"]
    expected = {(profile, operation, repetition)
                for profile in manifest["profiles"]
                for operation in manifest["operations"]
                for repetition in range(1, repetitions + 1)}
    text_fields = {"profile", "operation", "phase"}
    for row in rows:
        key = (row["profile"], row["operation"], int(row["repetition"]))
        require(key not in index, f"Duplicate measurement: {key}")
        require(key in expected and row["phase"] == "measurement",
                f"Unexpected measurement: {key}")
        require(tuple(int(row[field]) for field in ("n", "batch", "concurrency")) ==
                tuple(point[field] for field in ("n", "batch", "concurrency")),
                f"Incorrect point parameters: {key}")
        for field, value in row.items():
            if field not in text_fields:
                require(np.isfinite(float(value)) and float(value) >= 0,
                        f"Invalid measurement {field}: {key}")
        require(int(row["elapsed_ns"]) > 0 and int(row["attempts"]) > 0,
                f"Empty measurement: {key}")
        require(int(row["failed_calls"]) == int(row["unknown_updates"]) == 0,
                f"Unsuccessful measurement: {key}")
        rate = int(row["attempts"]) * 1e9 / int(row["elapsed_ns"])
        require(np.isclose(float(row["successful_rpcs_per_second"]), rate,
                           rtol=1e-11, atol=1e-7), f"Inconsistent throughput: {key}")
        if row["operation"] in ("insert", "modify"):
            require(np.isclose(float(row["confirmed_updates_per_second"]),
                               point["batch"] * rate, rtol=1e-11, atol=1e-7),
                    f"Inconsistent update throughput: {key}")
        index[key] = row
    require(set(index) == expected, f"Incomplete measurement coverage: {point['id']}")
    return index


def summarize(values, indices):
    values = np.asarray(values, dtype=np.float64)
    require(len(values) >= 2 and np.isfinite(values).all(), "Invalid sample")
    mean = float(np.mean(values))
    deviation = float(np.std(values, ddof=1))
    low, high = np.percentile(np.mean(values[indices], axis=1),
                              [2.5, 97.5], method="linear")
    return dict(rounds=len(values), mean=mean, median=float(np.median(values)),
                sample_stddev=deviation, cv_percent=100 * deviation / mean if mean else "",
                minimum=float(values.min()), maximum=float(values.max()),
                mean_ci95_low=float(low), mean_ci95_high=float(high))


def compare_summary(actual, expected, context):
    require(int(expected["rounds"]) == actual["rounds"], f"Incorrect round count: {context}")
    checked = 0
    for field in SUMMARY_FIELDS[1:]:
        value = actual[field]
        if value == "":
            require(expected[field] == "", f"Undefined CV must be empty: {context}")
        else:
            require(np.isfinite(float(expected[field])) and
                    np.isclose(float(expected[field]), value, rtol=1e-11, atol=1e-7),
                    f"Statistic differs for {context}, {field}")
            checked += 1
    return checked


def paired_values(index, pair, operation, repetitions):
    numerator, denominator = pair.split("/")
    return [float(index[numerator, operation, repetition]["successful_rpcs_per_second"]) /
            float(index[denominator, operation, repetition]["successful_rpcs_per_second"])
            for repetition in range(1, repetitions + 1)]


def verify_files(root, manifest):
    expected_paths = {f"data/rpc/{point['id']}/{name}"
                      for point in manifest["points"]
                      for name in ("measurement-rounds.csv", "round-statistics.csv",
                                   "paired-ratios.csv")}
    require(set(manifest["files"]) == expected_paths, "Incorrect CSV inventory")
    for name, expected in manifest["files"].items():
        path = root / name
        require(path.resolve().is_relative_to(root.resolve()), "CSV path outside dataset")
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected["sha256"],
                f"SHA-256 differs: {name}")
        require(len(raw) == expected["bytes"] and len(read_csv(path)) == expected["rows"],
                f"File dimensions differ: {name}")


def recompute(root, output):
    manifest = json.loads((root / "data/rpc/manifest.json").read_text())
    verify_files(root, manifest)
    repetitions = manifest["repetitions"]
    rng = random.Random(manifest["bootstrap"]["seed"])
    indices = np.array([[rng.randrange(repetitions) for _ in range(repetitions)]
                        for _ in range(manifest["bootstrap"]["resamples"])])
    metrics, pairs = [], []
    numeric_fields = measurement_rows = 0
    for point in manifest["points"]:
        base = root / "data/rpc" / point["id"]
        index = load_rounds(base / "measurement-rounds.csv", point, manifest)
        measurement_rows += len(index)
        saved_metrics = read_csv(base / "round-statistics.csv")
        expected_keys = {(profile, operation, metric)
                         for profile in manifest["profiles"]
                         for operation in manifest["operations"]
                         for metric in manifest["metrics"]}
        keys = [(row["profile"], row["operation"], row["metric"]) for row in saved_metrics]
        require(len(keys) == len(set(keys)) and set(keys) == expected_keys,
                f"Incorrect metric coverage: {point['id']}")
        for row in saved_metrics:
            values = [float(index[row["profile"], row["operation"], rep][row["metric"]])
                      for rep in range(1, repetitions + 1)]
            result = summarize(values, indices)
            numeric_fields += compare_summary(result, row, (point["id"], row["metric"]))
            metrics.append(dict(point=point["id"], profile=row["profile"],
                                operation=row["operation"], metric=row["metric"], **result))
        saved_pairs = read_csv(base / "paired-ratios.csv")
        keys = [(row["pair"], row["operation"]) for row in saved_pairs]
        expected_keys = {(pair, operation) for pair in ("sdk/raw-current", "legacy/raw-legacy")
                         for operation in manifest["operations"]}
        require(len(keys) == len(set(keys)) and set(keys) == expected_keys,
                f"Incorrect pair coverage: {point['id']}")
        for row in saved_pairs:
            require(row["metric"] == "successful_rpcs_per_second_ratio", "Incorrect ratio metric")
            result = summarize(paired_values(index, row["pair"], row["operation"], repetitions),
                               indices)
            numeric_fields += compare_summary(result, row, (point["id"], row["pair"]))
            pairs.append(dict(point=point["id"], pair=row["pair"],
                              operation=row["operation"], metric=row["metric"], **result))
    report = dict(passed=True, parameter_points=len(manifest["points"]),
                  measurement_rows=measurement_rows, metric_summaries=len(metrics),
                  paired_summaries=len(pairs), numeric_fields_checked=numeric_fields,
                  bootstrap=manifest["bootstrap"], numpy_version=np.__version__,
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  manifest_sha256=hashlib.sha256((root / "data/rpc/manifest.json").read_bytes()).hexdigest(),
                  files_sha256={name: value["sha256"] for name, value in manifest["files"].items()})
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in (("round-statistics.csv", metrics), ("paired-ratios.csv", pairs)):
        with (output / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/rpc")
    args = parser.parse_args()
    require(not args.output.resolve().is_relative_to(ROOT / "data"),
            "Output must be outside the source data directory")
    report = recompute(ROOT, args.output)
    print(json.dumps({key: value for key, value in report.items()
                      if key != "files_sha256"}, indent=2))


if __name__ == "__main__":
    main()
