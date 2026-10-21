# Construction data dictionary

The [construction method](construction.md) describes the workloads, measurement scope and statistics. The archived CSV files retain their original values and line endings.

## measurement-rounds.csv

There is one row per condition and repetition, with 270 rows in total.

| Column | Meaning |
| --- | --- |
| `sequence` | Execution position, from 1 to 270 |
| `kind` | `entry` for complete construction or `component` for a separate SDK operation |
| `profile` | `sdk`, `raw-current`, `legacy`, `raw-legacy`, `finsy` or `shell` |
| `workload` | `exact` or `mixed` |
| `mode` | `new` or `reused` for entry construction, empty for a public-operation condition |
| `component` | SDK public-operation name, empty for complete construction |
| `repetition` | Independent repetition block, from 1 to 10 |
| `iterations` | Number of timed invocations |
| `elapsed_ns` | Total time in the measurement loop, in nanoseconds |
| `ns_per_op` | `elapsed_ns / iterations` |
| `bytes_per_op` | Go benchmark's integer B/op result, empty for Python profiles |
| `allocs_per_op` | Go benchmark's integer allocs/op result, empty for Python profiles |

The five columns `kind`, `profile`, `workload`, `mode` and `component` identify a condition. Each condition has exactly ten rows. Empty Python allocation fields mean that those metrics were not collected. They do not represent zero allocations.

## round-statistics.csv

The first five columns identify the condition, and `metric` names the measurement column. Each of the 19 Go conditions has three metrics. Each of the eight Python conditions has one. This gives 65 summary rows.

| Column | Meaning |
| --- | --- |
| `rounds` | Number of independent repetitions, always 10 |
| `mean`, `median` | Arithmetic mean and median across rounds |
| `sample_stddev` | Sample standard deviation, with a divisor of 9 |
| `minimum`, `maximum` | Smallest and largest round values |
| `mean_ci95_low`, `mean_ci95_high` | 95% percentile-bootstrap interval for the mean |

## paired-ratios.csv

`workload`, `numerator`, `numerator_mode`, `denominator` and `denominator_mode` identify a pair. The metric is `construction_time_ratio`. Each ratio divides the numerator's `ns_per_op` by the denominator's value in the same repetition. The remaining columns summarize the ten ratios using the definitions above. There are sixteen rows.

## Output records and inventory

`records.jsonl` has one selected process record per measurement, in execution order. It retains the condition, repetition, sequence, exit code, process duration, logical CPU and benchmark metrics. `stderr_bytes` records the size of the corresponding stderr file. All 270 runs have exit code zero and empty stderr.

`metrics.ns_per_op` retains the measured ratio. Go records also retain `reported_ns_per_op`, the rounded value from the standard benchmark display. Python records retain GC status and interpreter version. Process duration includes setup and calibration outside the timed measurement loop.

`stdout/NNN.txt` is the output associated with sequence NNN. Go output retains the benchmark name, count, elapsed time, rounded ns/op and allocation metrics. Python output is JSON. Local interpreter paths are removed as described in the method.

The manifest identifies the selected source run, original run-manifest hash, 27 conditions, bootstrap method and source commits. Each archived file has a byte count, exported SHA-256, source filename and original SHA-256. Transformed exports also record their selected or removed fields. The figure metadata records the font, dimensions, resolution and source-statistics hash.
