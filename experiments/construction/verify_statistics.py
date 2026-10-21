"""Independent standard-library reference for means and bootstrap intervals."""

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics

import numpy as np


def csv_rows(path):
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def quantile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def check(row, values, indices):
    assert len(values) == int(row["rounds"]) == 10
    means = sorted(math.fsum(values[i] for i in sample) / 10 for sample in indices)
    expected = dict(mean=statistics.mean(values), median=statistics.median(values),
                    sample_stddev=statistics.stdev(values), minimum=min(values), maximum=max(values),
                    mean_ci95_low=quantile(means, 0.025), mean_ci95_high=quantile(means, 0.975))
    for key, value in expected.items():
        assert math.isclose(float(row[key]), value, rel_tol=1e-9, abs_tol=1e-8), (key, row, value)


def main(output):
    manifest = json.loads((output / "manifest.json").read_text())
    raw = [json.loads(line) for line in (output / "measurements.jsonl").read_text().splitlines()]
    index = {(r["kind"], r["profile"], r["workload"], r["mode"], r["component"], r["repetition"]): r for r in raw}
    assert len(index) == len(raw) == 270
    sample_indices = np.random.default_rng(manifest["plan"]["seed"]).integers(0, 10, size=(20000, 10)).tolist()
    checked = 0
    for row in csv_rows(output / "round-statistics.csv"):
        prefix = tuple(row[k] for k in ("kind", "profile", "workload", "mode", "component"))
        values = []
        for repetition in range(1, 11):
            metrics = index[(*prefix, repetition)]["metrics"]
            values.append(metrics["elapsed_ns"] / metrics["iterations"] if row["metric"] == "ns_per_op" else metrics[row["metric"]])
        check(row, values, sample_indices)
        checked += 1
    for row in csv_rows(output / "paired-ratios.csv"):
        values = []
        for repetition in range(1, 11):
            numerator = index["entry", row["numerator"], row["workload"], row["numerator_mode"], "", repetition]["metrics"]
            denominator = index["entry", row["denominator"], row["workload"], row["denominator_mode"], "", repetition]["metrics"]
            values.append((numerator["elapsed_ns"] / numerator["iterations"]) /
                          (denominator["elapsed_ns"] / denominator["iterations"]))
        check(row, values, sample_indices)
        checked += 1
    assert checked == 81
    inputs = [output / name for name in ("manifest.json", "measurements.jsonl", "measurement-rounds.csv",
                                         "round-statistics.csv", "paired-ratios.csv")]
    record = dict(verified_local=datetime.now().astimezone().isoformat(), summaries_verified=checked,
                  numerical_fields_verified=checked * 7, resamples=20000,
                  reference="math.fsum, statistics and explicit sorted linear-interpolation quantiles",
                  inputs_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (output / "summary-crosscheck.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k not in ("inputs_sha256", "script_sha256")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    main(parser.parse_args().output)
