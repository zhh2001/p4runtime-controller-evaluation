# Data-plane case studies

The E5 archive contains 130 functional cases recorded on 2026-10-19 using two Mininet hosts and BMv2. It covers reactive L2 forwarding, batched table updates with counter collection and application recovery, and the p4ctl CLI. Both hosts save transmitted and received frames. The archive includes all 520 PCAP files and 2,420 case files.

## Verify the archive

Use Python 3.12. This analysis uses only the Python standard library:

```sh
python3 scripts/verify_dataplane.py --output results/dataplane
python3 -m unittest discover -s tests/dataplane -v
```

The output directory must be new. Verification checks the complete file and archive-member inventories, compressed and uncompressed hashes, the randomized schedule, case coverage and original-source correspondence. It reconstructs the expected frames and checks complete payloads, identifiers and receiving ports. It reproduces `case-results.csv` and `cli-invocations.csv` byte for byte, including the documented command-path replacement.

The second implementation independently decodes PCAP headers and frames, host and driver command/reply logs, protobuf-JSON table readbacks, counters, target restart records and CLI invocations. It compares its per-case results with the original analyzer's results. The analysis runs without Mininet, BMv2, Go, external Python packages or root privileges.

## Topology and software

The topology has two isolated Mininet hosts connected to data ports 1 and 2 of one BMv2 switch. Device ID is 1 and CPU port is 255. [topology.json](../data/dataplane/topology.json) retains the recorded interface names and namespace PIDs. Target logs retain interface bindings, start and stop records, and exit codes. The run manifest records completed network cleanup.

The P4 program is the L2 fixture from SDK v2.0.1, revision `3f2def2b28c098961078ce64e9d86ab3c79e6ccb`. Its default L2 action sends unmatched frames to the CPU. `forward` updates the indirect counter indexed by output port and the direct table counter. PacketOut selects its explicit output port and bypasses these counter actions. The archive verifies the indirect counter used by the applications.

The six profiles and their dependency stacks match the [RPC comparison](rpc-method.md#software-and-host). They use SDK v2.0.1, the pinned legacy Go client, Finsy v0.30.0 and the pinned shell, plus two raw Go protocol profiles. p4ctl is tested separately at v2.0.1. A common application calls each controller adapter through a JSON process protocol. The [environment record](../data/dataplane/environment.json) retains dependency revisions, Python packages, kernel, CPU and memory details, and compiler, switch and Mininet versions. The host is WSL2 with an 8 GiB memory limit and 2 GiB swap. Mininet is 2.3.0.

Within each of ten repetitions, the six profiles are shuffled using seed 20261017. Each profile runs reactive forwarding followed by counter recovery. Ten CLI cases follow. The [formal plan](../data/dataplane/plan.json) and unchanged [run manifest](../data/dataplane/run.json) retain this schedule and its completion labels. Smoke and race checks are outside this selected archive.

## Reactive forwarding

Each case begins with a fresh target and no L2 rules. The application sends one frame from each direction, waits for its PacketIn, installs the known destination-MAC mapping and sends that original frame through PacketOut. It then sends 32 additional frames in each direction through the installed rules.

All sixty reactive cases receive exactly two PacketIn messages and forward all 66 frames. Table readbacks verify each installed rule. Each output counter records 32 packets and 2,048 bytes. The two PacketOut first frames bypass the counter branch in this P4 program.

## Counter collection and recovery

Each case installs sixteen destination-MAC rules and sends eight frames per rule, for 128 frames. A single sixteen-entry MODIFY batch swaps the output ports, followed by another 128 frames. Counter polling uses a configured 50 ms interval. Checkpoints compare the adapter's counter results with independent observer reads. Per-port counts progress from zero to 64 and then 128 packets, with 64 bytes per packet.

The application stops BMv2 and sends one probe frame, which must not arrive at either host. It then closes the old controller session, starts a fresh target, confirms the missing pipeline, installs the configuration, opens a new session and verifies zero counters. It restores the swapped rules and sends the final 128 frames. Each output counter reaches 64 packets and 4,096 bytes.

All sixty cases verify the restored rules and 384 forwarded frames, with one expected stopped-target drop per case. The restart recovery belongs to this common application. The archive contains 2,040 periodic counter samples and sixty target restarts.

## CLI cases

Each CLI case invokes version, pipeline set, two table inserts, two counter reads and PacketOut, for seven process calls. Sixteen host-generated frames verify table forwarding and output counters. The CLI then injects a seventeenth frame with an explicit output of port 2 even though its destination-MAC rule selects port 1. The receiving PCAP confirms the explicit PacketOut port. Counters remain at eight packets and 512 bytes per port.

All seventy CLI calls succeed. The invocation CSV preserves elapsed times and exit codes. Each elapsed time includes process startup, argument handling and the command's work. Version calls provide the separately labelled operation that does not access the target. The independent verifier recomputes each elapsed interval from its recorded start and end timestamps and decodes the JSON counter output.

## Results

| Profile | Reactive cases | Counter/recovery cases | CLI cases | Received frames | Target restarts |
| --- | ---: | ---: | ---: | ---: | ---: |
| sdk | 10 | 10 | 0 | 4,500 | 10 |
| raw-current | 10 | 10 | 0 | 4,500 | 10 |
| legacy | 10 | 10 | 0 | 4,500 | 10 |
| raw-legacy | 10 | 10 | 0 | 4,500 | 10 |
| finsy | 10 | 10 | 0 | 4,500 | 10 |
| shell | 10 | 10 | 0 | 4,500 | 10 |
| p4ctl | 0 | 0 | 10 | 170 | 0 |

All 130 cases pass. The 27,220 host-transmitted frames plus ten CLI-injected frames, less sixty expected stopped-target drops, account for all 27,170 received frames. The checks detect no additional missing, duplicate or wrong-port frames. All 360 counter checkpoints match the expected traffic and independent reads. The records contain 180 normally exited controller processes and 190 target generations, including the sixty restarts.

These cases validate functionality through a common application and adapter protocol. They do not rank native controller first-frame latency. Session-level reconnection is evaluated separately in the [session comparison](sessions.md).

## Archive and source correspondence

Seven compressed archives group cases by profile. They preserve all original PCAP bytes, driver and host output, commands, case results, events and target logs. Text replacements affect the original local workspace prefix in 130 event files and the CLI invocation CSV. `<workspace>` replaces that prefix. Other text bytes remain unchanged. The catalog retains original and exported hashes and replacement counts. PIDs, loopback ports, timestamps, measured values and frame payloads are retained.

[dataplane-source-provenance.json](../config/dataplane-source-provenance.json) binds the extracted analysis code to the formal execution snapshot. The original frame generator, PCAP reader, frame comparison, expected-workload generator and per-case checker retain their function bodies. All four original frame tests also retain their bodies. Live process helpers and private report generation are omitted from the offline package. The additional decoder and corruption tests cross-check host logs, command/reply correspondence, decoded tables, raw counter replies, batch update intent and explicit CLI PacketOut ports.

The [data dictionary](dataplane-data.md) describes the catalog, record fields and generated outputs. This batch provides archived records and offline verification. Live data-plane runners will follow separately.
