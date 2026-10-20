# RPC data dictionary

Each parameter-point directory contains three CSV files. CSV values are copied without modification from the completed run selected for that point. Git preserves the original CSV line endings so that checkout does not change the recorded file hashes.

## measurement-rounds.csv

There is one row per profile, repetition and operation, with 240 rows per point. All rows have `phase=measurement`.

| Column | Meaning |
| --- | --- |
| `profile` | Software configuration, as listed in the method |
| `repetition` | Repetition block, from 1 to 10 |
| `n`, `batch`, `concurrency` | Point parameters N, B and C |
| `operation` | `insert`, `modify`, `read_one` or `read_all` |
| `phase` | Selected measurement phase |
| `elapsed_ns` | Actual workload duration in nanoseconds |
| `attempts` | Number of attempted RPCs |
| `p50_ns`, `p95_ns`, `p99_ns` | Per-round total request-latency percentiles |
| `successful_rpcs_per_second` | Successful RPC count divided by the load window |
| `confirmed_updates_per_second` | Confirmed write updates divided by the load window |
| `failed_calls` | Failed RPC count |
| `unknown_updates` | Updates with an unknown result |
| `read_entities_per_second` | Returned entity count divided by the load window |
| `build_mean_ns`, `build_p50_ns`, `build_p95_ns`, `build_p99_ns` | Request-construction time before the public call |
| `call_mean_ns`, `call_p50_ns`, `call_p95_ns`, `call_p99_ns` | Time in the public call |
| `client_cpu_core_percent`, `target_cpu_core_percent` | Process CPU time divided by the outer observation interval, relative to one logical core |
| `client_peak_rss_kib`, `target_peak_rss_kib` | Maximum sampled process RSS, in KiB |
| `client_observation_seconds`, `target_observation_seconds` | Outer intervals used for resource measurements |

`concurrency` records the parameter point. Read-all still runs with one worker. All saved measurement rows have zero failed calls and zero unknown updates.

## round-statistics.csv

The file has 312 rows: six profiles, four operations and thirteen metrics. The manifest lists the thirteen summarized columns.

| Column | Meaning |
| --- | --- |
| `profile`, `operation`, `metric` | Group and measurement column |
| `rounds` | Number of repetition blocks, always 10 |
| `mean`, `median` | Arithmetic mean and median across rounds |
| `sample_stddev` | Standard deviation with a divisor of 9 |
| `cv_percent` | 100 times standard deviation divided by the mean |
| `minimum`, `maximum` | Smallest and largest round values |
| `mean_ci95_low`, `mean_ci95_high` | Percentile-bootstrap interval for the mean |

An empty `cv_percent` means that the mean is zero and the coefficient is undefined. It is not a missing observation. All other numeric fields are finite.

## paired-ratios.csv

The file has eight rows: two matched Go pairs and four operations. `pair` identifies the numerator and denominator. `metric` is `successful_rpcs_per_second_ratio`. The remaining columns have the same meanings as above, applied to the ten within-repetition ratios.

## manifest.json

The manifest records parameter values, profile order within each repetition, the bootstrap settings and file hashes. Each CSV entry includes its row count, byte count and relative path in the original experiment records.
