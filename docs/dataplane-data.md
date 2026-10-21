# Data-plane data dictionary

A case is identified by repetition, profile and kind. Directory labels use `NN-profile-kind`, where `kind` is `reactive`, `counter-recovery` or `cli`. The global `trial` is the one-based position in the run schedule. All cases use 64-byte Ethernet frames without FCS.

## Archive files

| File | Contents |
| --- | --- |
| `manifest.json` | File and member sizes, original and exported hashes, archive metadata and transformations |
| `records/<profile>.tar.gz` | Complete selected case directories for one profile |
| `run.json` | Unchanged original run settings, randomized schedule, completion labels, cleanup status and source hashes |
| `plan.json` | Original fixed counts, ports, polling interval, repetitions and version |
| `environment.json` | Original host, package, source-revision and tool-version records |
| `topology.json` | Recorded host/interface names, namespace PIDs and switch interfaces |
| `recorded-verification.json` | Original totals and hashes for all 2,420 case files |
| `case-results.csv` | Original per-case functional counts |
| `cli-invocations.csv` | Original CLI calls with the local workspace prefix replaced by `<workspace>` |
| `fixtures/l2.p4` | Original L2 P4 source |
| `fixtures/l2.p4info.txt` | Shared compatible P4Info used by the controller profiles |
| `fixtures/l2.compiler.txtpb` | Original compiler P4Info, including `initial_default_action` |
| `fixtures/schema.json` | Table, match, action-parameter and counter declarations projected from the compatible P4Info |

Gzip timestamps and tar ownership/timestamps are zero. Files use mode 0644 and name order. Tar members are regular files with relative case paths. The verifier checks the container hashes and every member before writing an analysis copy. PCAPs and other unchanged members must match their original hashes in `recorded-verification.json`. Replaced text members retain both hashes and the number of replaced path prefixes.

The original source inventory includes execution sources and binaries whose hashes are retained but whose files are outside this offline package. The P4Info projection records its source hash and was generated using the recorded protobuf definitions.

## Per-case files

| Path | Fields and purpose |
| --- | --- |
| `h1/tx.pcap`, `h2/tx.pcap` | Frames sent by the two host processes |
| `h1/rx.pcap`, `h2/rx.pcap` | Frames received on each host's interface |
| `hostN.jsonl`, `hostN-commands.json` | Host readiness, sent/received payloads, commands and replies |
| `hostN.stderr` | Host process diagnostics |
| `driver-N.jsonl`, `driver-N-commands.json` | Controller readiness, PacketIn, commands and replies for generation N |
| `driver-N.stderr` | Controller process diagnostics |
| `events.jsonl` | Application events, observer table/counter reads, checkpoints, periodic samples, restart phases and CLI calls |
| `target-processes.json`, `target-N.log` | Target commands, interfaces, starts/stops, exit codes and logs |
| `result.json` | Original case identity, expected traffic, captured counts and process/generation counts |

Frames use EtherType `0x88b5`, marker `E5PKT001`, a big-endian 32-bit trial ID and sequence ID, and fixed `a5` padding. The six-byte source MAC identifies source host 1 or 2. Expected destinations and receiving ports follow the case workload. The capture format is little-endian PCAP 2.4 with Ethernet link type 1. Both decoders compare complete payloads and receiving ports, including multiplicity.

Driver and host commands use sequential `id` values. Replies identify the same IDs and carry `ok`. Driver counter replies contain packet and byte counts. The verifier associates every periodic sample and checkpoint read with the corresponding generation's raw counter replies. JSON table snapshots include both decoded MAC/port entries and original protobuf-JSON Read responses. Byte fields in these responses use base64.

Events use WSL monotonic nanoseconds. Periodic samples record driver generation, counter index, packets, bytes and the configured interval. Target history records separate start and stop times for each generation. CLI events include the argument vector, stdout, stderr, exit code, start and end times, and elapsed nanoseconds.

## CSV and regenerated outputs

`case-results.csv` contains profile, case and trial, followed by host-transmitted frames, received frames, expected drops, PacketIn count, counter checkpoints, periodic samples, target restarts, exited controller processes and CLI invocation count. CLI-injected frames appear in received frames and are outside host-transmitted frames.

`cli-invocations.csv` contains the joined command, `elapsed_ns`, `exit_code` and `version_only`. Values describe each complete process invocation. The CSV files preserve their original CRLF line endings.

The verifier creates the case directories in the requested new output directory, reproduces both CSV files and writes `independent-verification.json` and `verification.json`. The independent report includes reconstructed per-case counts and totals. The final report records the case, PCAP and file counts, source correspondence and catalog hash. No archived files are modified. See the [method](dataplane.md) for traffic, counter and recovery behavior.
