# evaluation-v1

This snapshot contains the evaluation data, analysis scripts and live runners for p4runtime-go-controller v2.0.1. Its source commit is [`edbb75d7b83d27dce6a3138aa01122921773f7ab`](https://github.com/zhh2001/p4runtime-controller-evaluation/tree/edbb75d7b83d27dce6a3138aa01122921773f7ab).

## Contents

| Dataset | Coverage |
| --- | --- |
| RPC performance | 1,680 measurements across seven parameter points, six profiles and ten repetition blocks |
| Local construction | 270 measurements, 65 metric summaries and sixteen paired summaries |
| Input validation | 382 fixed cases and 6,984 trials |
| Sessions and streams | 840 trials across 28 configurations and thirty repetitions |
| Batch-write retries | 192 application trials across six profiles and sixteen target-specific scenarios |
| Data-plane cases | 130 Mininet/BMv2 cases, 520 PCAP files and 27,170 received frames |

The archive includes recorded inputs and observations, fixtures, dependency locks, source correspondence, tests, saved figures, statistical reanalysis and functional verification. Live runners cover all six workloads. The recorded environment is Linux x86_64 on WSL2.

The original RPC request records are distributed separately in the [rpc-traces-v1 Release](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/rpc-traces-v1). Those seven archives cover all 2,940 measurement and warmup phases. Their sizes and hashes are included in this snapshot's catalog and the release manifest.

## Download and verify

Place `evaluation-v1.tar.gz`, `evaluation-v1-manifest.json` and `SHA256SUMS` in one directory, then run:

```sh
sha256sum -c SHA256SUMS
tar -xzf evaluation-v1.tar.gz
cd p4runtime-controller-evaluation-v1
```

Follow `docs/reproducing-results.md` for analysis dependencies, offline checks, test suites, RPC trace downloads and live execution. The archive works without Git metadata. Live runs require the tools listed in each execution guide.

The manifest records every source-snapshot file's size, SHA-256 hash, Git blob ID and mode. The [packaging guide](https://github.com/zhh2001/p4runtime-controller-evaluation/blob/main/docs/releases.md) provides file-level checking with the release helper from a current checkout.

## Citation and license

For a versioned citation, use:

Zhang H, Yang T, Chen J. P4Runtime Controller Evaluation. 2026. GitHub. Version evaluation-v1. [Release permalink](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/evaluation-v1).

Source code, tests, configuration and documentation use Apache-2.0. Experimental data and derived figures use CC BY 4.0. See [LICENSE.md](https://github.com/zhh2001/p4runtime-controller-evaluation/blob/edbb75d7b83d27dce6a3138aa01122921773f7ab/LICENSE.md) for the file scopes and attribution.
