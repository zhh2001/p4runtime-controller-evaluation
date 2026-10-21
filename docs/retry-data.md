# Batch-retry data dictionary

Trials are identified by target `mode`, profile, repetition and case ID. A `token` encodes the last three as `repetition|profile|case_id` and associates observations with RPC records. Each target records two repetitions of every selected case/profile pair.

## Files

| File | Purpose |
| --- | --- |
| `manifest.json` | Selected runs, file sizes, SHA-256 hashes and export transformations |
| `selection.json` | Original relative directories of the two formal runs |
| `plan.json` | Profiles, two repetitions, case counts, attempt bound, watchdog and locked case hash |
| `fixtures/cases.json` | Original updates, initial seeds, injected schedules and response forms |
| `fixtures/keys.p4`, `fixtures/keys.p4info.txt` | P4 program and declarations used by the trials |
| `<mode>/run.json` | Unchanged original run manifest, completion fields, versions and source inventory |
| `<mode>/source-artifact-hashes.json` | Unchanged original artifact inventory, including hashes for files not exported in this batch |
| `<mode>/observations.jsonl.gz` | One application observation per trial |
| `<mode>/rpc.jsonl.gz` | Tested-path Write and application Read records |
| `<mode>/driver-logs.jsonl.gz` | Original stdout and stderr as `file`, `text` and `sha256` records |
| `<mode>/recorded-analysis.json` | Historical primary reconstruction and per-trial results |
| `<mode>/recorded-independent.json` | Historical independent wire-decoder report |
| `bmv2/target-processes.json` | Target start, stop and exit history |
| `counts.csv` | Per-target counts across all profiles and both repetitions |
| `profile-outcomes.csv` | Per-target/profile outcome counts across both repetitions |
| `trials.csv` | One regenerated row per application trial |

Gzip streams use timestamp zero. Observation and RPC decompression preserves the original bytes and row order. Protobuf byte fields use base64, and protobuf 64-bit integer fields use JSON strings. Driver log text is UTF-8 and preserves the original file bytes when encoded again.

## Observations and requests

`case.updates` contains operations with `original_index`, `key`, `type`, `port` and `metadata`. Each original index refers to a position in the initial application batch. `case.seeds` defines prior entries. Controlled `schedule` arrays give the per-update canonical codes for successive requests, in each request's local order.

`steps` records each attempt's `pending` subset, driver `result`, independent `after` snapshot, complete `codes` when available, and `status_source`. A non-returning driver instead records `watchdog_timeout` and `watchdog_seconds`. `application_read` stores a returned reconciliation Read. `decision.pending` holds the next subset with original indexes intact. `decision.blocked` and the trial-level `blocked` list identify permanent failures.

`before`, step `after` and trial `after` contain both protobuf-JSON `entities` and base64-encoded binary Entity messages. The decoders compare table keys, action ports and metadata, including the preserved state of blocked and terminated trials. `driver_exit` is zero for returned applications and -9 for watchdog terminations.

RPC records have an increasing `sequence`, the application token, `kind`, numeric top-level `rpc_code`, JSON `request` and base64 `request_binary`. Writes may have a `raw_status` trailer. Reads include `responses` and `response_binary`. Setup, arbitration and independent observer Reads are outside this tested-path log.

Go's error conversion also serializes a status for RPC-wide errors that have no status-details trailer. Such driver `raw_status` fields contain the top-level code and message with no per-update details. They do not establish individual update outcomes.

For an RPC-wide OK, `codes` contains one zero per update. For a complete valid per-update failure response, it contains the ordered canonical codes. For an RPC-wide error or malformed/incomplete failure details, it is null and the application reads target state. These rules differ from a failed-index-only map, which omits successful items.

## Summary fields

`counts.csv` distinguishes `trials`, `completed`, `blocked`, `watchdog_timeout`, `write_requests`, `update_attempts`, `successful_updates`, `failed_updates` and `application_reads`. Update counts include retry requests. `profile-outcomes.csv` splits the three trial outcomes by profile and target. These are counts of fixed functional scenarios.

`trials.csv` includes mode, repetition, profile, case ID, outcome, Write count, application Read count, update-attempt count and `final_state_matches_desired`. The last field compares the observed state with the full original intent. It can be false for a blocked or watchdog-terminated trial. Both kinds of partial state are still verified against the actual successful updates.

The verifier writes decompressed records and fixture snapshots into the requested new output directory, reproduces the two historical reports for each target and all three CSV files, and writes `verification.json` with totals and archive/source-provenance hashes. It leaves archived files untouched. See the [method](retries.md) for policy and results.
