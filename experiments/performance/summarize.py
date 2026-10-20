"""Per-phase metrics and across-round statistics for direct BMv2 runs."""

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics as stats

from audit import percentile, statistics
from analyze import source_check, analyze_workloads
from common import PAPER


def linear_quantile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_indices(n, count, seed):
    rng = random.Random(seed)
    return [[rng.randrange(n) for _ in range(n)] for _ in range(count)]


def summarize(values, indices):
    assert len(values) > 1 and all(math.isfinite(value) for value in values)
    mean = stats.fmean(values)
    deviations = stats.stdev(values)
    bootstrap = [stats.fmean(values[index] for index in sample) for sample in indices]
    return {"rounds": len(values), "mean": mean, "median": stats.median(values),
            "sample_stddev": deviations, "cv_percent": 100 * deviations / mean if mean else None,
            "minimum": min(values), "maximum": max(values),
            "mean_ci95_low": linear_quantile(bootstrap, .025),
            "mean_ci95_high": linear_quantile(bootstrap, .975)}


def resources(row):
    result = {}
    record = row["resources"]
    for name in ("client", "target"):
        before, after = record["before"][name], record["after"][name]
        seconds = (after["monotonic_ns"] - before["monotonic_ns"]) / 1e9
        assert seconds > 0
        ticks = sum(after[field] - before[field] for field in ("user_ticks", "system_ticks"))
        assert ticks >= 0
        result[name + "_cpu_core_percent"] = 100 * ticks / record["clock_ticks_per_second"] / seconds
        samples = [before["rss_kib"], after["rss_kib"]]
        samples.extend(sample[name]["rss_kib"] for sample in record["rss_samples"])
        assert all(value is not None and value > 0 for value in samples)
        result[name + "_peak_rss_kib"] = max(samples)
        result[name + "_observation_seconds"] = seconds
    return result


def metrics(row):
    result = statistics(row)
    records = row["records"]
    for name, start, end in (("build", "start_ns", "build_end_ns"),
                             ("call", "build_end_ns", "end_ns")):
        values = [record[end] - record[start] for record in records]
        result[name + "_mean_ns"] = stats.fmean(values)
        for label, q in (("p50", .5), ("p95", .95), ("p99", .99)):
            result[name + "_" + label + "_ns"] = percentile(values, q)
    result.update(resources(row))
    return result


def write_csv(path, rows):
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize_run(output):
    manifest = source_check(output)
    if manifest["kind"] == "oracle" or manifest["inject_failure"]:
        raise ValueError("Statistics require a direct BMv2 run without injected failures.")
    if manifest["repetitions"] < 2 or len(manifest["configurations"]) != 1:
        raise ValueError("Use one parameter point and at least two repetitions.")
    report = analyze_workloads(output, manifest)
    definition = json.loads((PAPER / "data/rpc/manifest.json").read_text())
    config = json.loads((PAPER / "config/rpc.json").read_text())
    if manifest["profiles"] != definition["profiles"]:
        raise ValueError("Statistics require all six profiles.")
    n, batch, concurrency = manifest["configurations"][0]
    point = next(item["id"] for item in config["points"]
                 if (item["n"], item["batch"], item["concurrency"]) == (n, batch, concurrency))
    phases = []
    for brief in map(json.loads, (output / "observations.jsonl").read_text().splitlines()):
        row = json.loads((output / brief["detail_file"]).read_text())
        phases.append({key: row[key] for key in
                       ("profile", "repetition", "n", "batch", "concurrency",
                        "operation", "phase", "elapsed_ns")} | metrics(row))
    write_csv(output / "phase-summary.csv", phases)
    measurements = [row for row in phases if row["phase"] == "measurement"]
    write_csv(output / "measurement-rounds.csv", measurements)
    index = {(row["profile"], row["operation"], row["repetition"]): row for row in measurements}
    rounds = manifest["repetitions"]
    indices = bootstrap_indices(rounds, config["bootstrap"]["resamples"], config["bootstrap"]["seed"])
    summaries = []
    pairs = []
    for profile in definition["profiles"]:
        for operation in definition["operations"]:
            for metric in definition["metrics"]:
                values = [index[profile, operation, rep][metric] for rep in range(1, rounds + 1)]
                summaries.append(dict(profile=profile, operation=operation, metric=metric,
                                      **summarize(values, indices)))
    for pair in ("sdk/raw-current", "legacy/raw-legacy"):
        numerator, denominator = pair.split("/")
        for operation in definition["operations"]:
            metric = "successful_rpcs_per_second"
            values = [index[numerator, operation, rep][metric] /
                      index[denominator, operation, rep][metric] for rep in range(1, rounds + 1)]
            pairs.append(dict(pair=pair, operation=operation, metric=metric + "_ratio",
                              **summarize(values, indices)))
    write_csv(output / "round-statistics.csv", summaries)
    write_csv(output / "paired-ratios.csv", pairs)
    sources = [Path(__file__), PAPER / "config/rpc.json", PAPER / "data/rpc/manifest.json"]
    hashes = {}
    for source in sources:
        name = str(source.relative_to(PAPER))
        target = output / "analysis-source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    report.update(parameter_point=point, measurement_rows=len(measurements),
                  metric_summaries=len(summaries), paired_summaries=len(pairs),
                  bootstrap=config["bootstrap"], summary_sources_sha256=hashes)
    (output / "summary-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_run(args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
