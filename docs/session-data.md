# Session and stream data dictionary

See the [method](sessions.md) for the plan, selection and experimental conditions. All four groups use `adapter` values `sdk` and `finsy`, and repetitions 1 through 30.

## Inventory and run metadata

`data/sessions/manifest.json` lists 42 files, their byte lengths and SHA-256 hashes. Compressed files also record uncompressed byte lengths, hashes and JSONL record counts. `source_file` and `source_sha256` refer to original files. `transformation` describes exports that change packaging or metadata.

Each `<group>/run.json` preserves the source run's kind, settings, plan, complete flag, trial count, randomized schedule, timestamps and executable hash. `execution_sources_sha256` contains the original source and binary inventory. `source_manifest_sha256` identifies the original manifest before omission of its executable path. The verifier reconstructs and checks the randomized schedule.

`selection.json` identifies the original directory for each selected group and the excluded first cancellation attempt. `environment.json` records dependency revisions, build information, kernel and memory state. Private workspace prefixes in build information are represented by `<workspace>`.

## Common trial and event fields

| Field | Meaning |
| --- | --- |
| `adapter`, `repetition` | Client profile and independent repetition |
| `mode` | Recovery fault, or cancellation/close scenario |
| `path`, `delay_ms` | Consumer path and per-message processing delay |
| `start_sequence`, `end_sequence` | Inclusive bounds of a trial's event slice |
| `driver_exit_code` | Driver process exit status |
| `driver_records` | Recorded application commands, replies and state observations, where present |
| `sequence` | Zero-based event index in that group's `rpc.jsonl` |
| `event` | Event name, such as arbitration response, Write completion or packet emission |
| `monotonic_ns` | Event timestamp from the WSL monotonic clock, in nanoseconds |
| `client_monotonic_ns` | Client-observed timestamp on the same host, where present |
| `rpc_code` | Canonical gRPC status code, with 0 indicating OK |
| `request`, `response` | Protobuf JSON mapping of recorded RPC messages, where present |

The observations and RPC JSONL files preserve their original row order. All sequence references are within their group's event file. Byte fields in protobuf JSON use base64, and 64-bit protobuf integers can be JSON strings.

`target-processes.json` preserves owned BMv2 process generations, commands, PIDs, start and stop timestamps and exit codes. Recovery and mastership have 240 and 60 target generations respectively. The controlled-service groups have empty lists.

`stderr.jsonl.gz` contains one record per driver process, with `file`, `text`, `source_sha256`, `text_sha256` and `local_path_replaced`. The text hash covers UTF-8 bytes. Only private Finsy source prefixes in recovery EOF tracebacks are replaced.

## Recovery and mastership

Recovery observations include initial and final writes and readbacks, arbitration and verification sequence references, the actual Write results, pipeline-preservation or restoration state, process history and driver replies.

`recovered` and `censored` distinguish verified recovery from a trial reaching the observation limit. A censored trial records `censor_ns`. Successful trials provide four intervals measured from the fault event:

| Field | End event |
| --- | --- |
| `fault_to_arbitration_ns` | Relay receives successful target arbitration |
| `fault_to_primary_observed_ns` | Application observes primary state |
| `fault_to_successful_write_ns` | Recovery Write completes successfully |
| `fault_to_verified_state_ns` | Independent Read verifies the recovery entry |

`recovery-trials.csv` projects these fields with adapter, mode and repetition. The archive has no censored trials.

Mastership observations include takeover, demotion, observed-backup and higher-controller-close sequence references. `backup_write_rpcs` counts actual writes during observed backup state. `demotion_to_observation_ns` is the interval from the target's demotion response to client observation. `writes`, `final_write` and `final_entries` retain the actual operations and final state.

## Cancellation and close

`mode` is `pre_cancelled`, `queued_cancelled`, `inflight_cancelled`, `immediate`, `graceful` or `graceful_timeout`. Finsy has the in-flight and immediate modes. `cancelled_send`, `send` and `close` retain the relevant driver replies. A reply's `cancelled`, `deadline` and `not_primary` fields describe mapped local results.

`received_packet_ids` lists actual service reads in order. `server_events` embeds the associated event records. Each received PacketOut records its ID, payload size, SHA-256 and metadata. ID 20000 denotes the cancelled request in pre-call and queued scenarios; ID 99999 is the later barrier. `request_eof_sequence` and `final_status_released_sequence` identify graceful-close phases. `outcomes.csv` groups trial counts and recovery or immediate-close results by adapter and mode.

## Slow consumers

| Field | Unit and meaning |
| --- | --- |
| `sent` | Number of emitted PacketIn messages, 300 per trial |
| `offered_rate_pps` | Offered messages per second, 500 |
| `application_capacity` | Application queue capacity, 50 for queued paths and null for native paths |
| `library_packet_queue_capacity` | Finsy's library queue capacity 50, or null for SDK |
| `final.arrived` | Unique IDs delivered to the application iterator or callback |
| `final.processed` | Unique IDs whose processing completed |
| `delivered` | Completed processing count, equal to the length of `final.processed` |
| `application_dropped` | Arrived IDs discarded by the application queue |
| `not_delivered_to_iterator` | Emitted IDs absent from `final.arrived` |
| `not_processed` | Sum of application drops and IDs not delivered to the iterator |
| `marker_to_observed_backup_ns` | Nanoseconds from service arbitration notification to observed backup state |
| `rss_samples` | Timestamped process RSS samples in KiB |
| `baseline_rss_kib` | First RSS sample |
| `peak_sampled_rss_kib` | Maximum recorded RSS sample |

`consumer-summary.csv` groups observed ranges and medians by adapter, path and delay. `rss-evaluation.jsonl` compares each recorded peak with the maximum of all original samples. All 360 peaks agree in this archive. The field named `delivered` counts completed processing, while `final.arrived` describes application receipt.

## Statistics

Each `statistics.csv` row identifies `group`, `adapter`, `setting`, `metric` and `unit`. Settings are the fault name, `takeover`, or `<path>/<delay_ms>`. Metrics use milliseconds, KiB or packet counts as specified by `unit`.

The columns are `n`, `mean`, `median`, `sample_stddev`, `minimum`, `maximum`, `p95`, `median_ci_low` and `median_ci_high`. Every row has `n=30`. Standard deviation uses the sample denominator. Quantiles use linear interpolation. The last two fields are percentile bootstrap limits for the median.

The bootstrap key is `group/adapter/setting/metric`. Its UTF-8 SHA-256 digest supplies the first four bytes as an unsigned big-endian integer, added to 20261019. Python's `random.Random` generates 10,000 size-30 samples with replacement. The independent NumPy calculation reuses the random draws and recomputes all eight numerical summary fields per row.

`summary.json` contains 28 configuration outcomes, group trial counts, packet totals, method settings and hashes of original inputs. The verification output preserves these outcomes and statistics, while its creation time and reconstructed input-file hashes describe the current analysis.
