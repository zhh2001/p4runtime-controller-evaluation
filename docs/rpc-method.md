# RPC performance method

Each configuration sends P4Runtime requests to BMv2 over loopback. The workload uses a table with a 32-bit EXACT key and a direct action with a 9-bit destination port. Table capacity is 32,768 entries.

## Workloads

`N` is the table population, `B` is the number of updates per Write request, and `C` is the number of concurrent workers.

| Point | N | B | C |
| --- | ---: | ---: | ---: |
| default | 4,096 | 16 | 1 |
| n256 | 256 | 16 | 1 |
| n16384 | 16,384 | 16 | 1 |
| b1 | 4,096 | 1 | 1 |
| b128 | 4,096 | 128 | 1 |
| c4 | 4,096 | 16 | 4 |
| c16 | 4,096 | 16 | 16 |

INSERT starts with an empty table and ends after inserting all N entries. MODIFY alternates action values on a populated table. Read-one selects one entry. Read-all reads the complete table with one worker. Consequently, C applies to INSERT, MODIFY and Read-one, while Read-all uses C=1 at every point.

MODIFY and both Read workloads use a five-second warmup followed by a 30-second measurement window. INSERT is a finite operation with no warmup. Workers issue closed-loop requests. At C greater than one, workers use disjoint key partitions for writes.

Each point has ten repetition blocks. Each block runs all six configurations in a shuffled order, recorded in the manifest. Elapsed times use WSL's CLOCK_MONOTONIC. Recorded throughput divides completed calls or confirmed updates by the actual load window, including completion of the final call. Full table readback checks the installed keys and action parameters after each phase.

## Software and host

| Profile | Implementation | gRPC | Protobuf |
| --- | --- | --- | --- |
| sdk | p4runtime-go-controller v2.0.1 | 1.83.2 | 1.36.11 |
| raw-current | Go generated RPCs, P4Runtime 1.5.0 | 1.83.2 | 1.36.11 |
| legacy | p4runtime-go-client, de3748d9d859 | 1.56.3 | 1.33.0 |
| raw-legacy | Go generated RPCs, P4Runtime 1.4.0-rc.5 | 1.56.3 | 1.33.0 |
| finsy | Finsy v0.30.0 | 1.81.1 | 6.33.6 |
| shell | p4runtime-shell, 5641d39c1778 | 1.84.0 | 3.20.3 |

The Go configurations use Go 1.26.9. The Python configurations use Python 3.12.3, with the upb protobuf backend for Finsy and the Python backend for the shell.

Measurements ran on an Intel Core i5-13500H Windows host under WSL2, with Linux 6.6.87.2, 16 visible logical CPUs, an 8 GiB memory limit and 2 GiB swap. The target was BMv2 1.15.0 at revision 2bdd0b7b, compiled P4 used p4c 1.2.5.10 at revision 8b6de3c57, and the RPC deadline was ten seconds. Configurations ran sequentially.

## Across-round statistics

The sampling unit is a repetition block. Summaries retain all ten rounds. The confidence interval for an arithmetic mean uses 20,000 percentile bootstrap samples with seed 20261017. Each sample draws ten repetition indices with replacement using Python's `random.Random`. All configurations and metrics reuse the same index vectors. The interval endpoints use linear interpolation at the 2.5th and 97.5th percentiles.

The matched ratios are `sdk/raw-current` and `legacy/raw-legacy`. Each ratio is computed within a repetition block. The reported statistic is the arithmetic mean of those ten ratios, with bootstrap resampling of the repetition blocks. It is not the ratio of the two configurations' means.

Per-round latency percentiles use nearest rank. Across-round summaries of those percentiles describe variation between rounds, rather than pooled request latency. Resource measurements include the outer command interval, which covers record buffering, serialization and interprocess communication. CPU usage is expressed relative to one logical core and can exceed 100%.

The reanalysis script recomputes the across-round statistics from the saved measurement rows. Per-request timing and full-state readback belong to the benchmark execution records.

## Sources

- [p4runtime-go-controller v2.0.1](https://github.com/zhh2001/p4runtime-go-controller/tree/3f2def2b28c098961078ce64e9d86ab3c79e6ccb)
- [p4runtime-go-client](https://github.com/antoninbas/p4runtime-go-client/tree/de3748d9d85953f1fa7093ca441aca53dfc9008b)
- [Finsy v0.30.0](https://github.com/byllyfish/finsy/tree/491a6e2ac67473ec2ee9a94601f6107ae5d76e7b)
- [p4runtime-shell](https://github.com/p4lang/p4runtime-shell/tree/5641d39c1778c79f726da7e216b2c7e134af4a47)
