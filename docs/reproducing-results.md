# Reproduce the evaluation results

The evaluation uses p4runtime-go-controller v2.0.1 and the client revisions in [dependencies.json](../config/dependencies.json). Start with offline verification to check the published records and recompute the statistics. The live runners generate new observations using the fixed workloads.

## Verify the published records

Use CPython 3.12 on Linux x86_64. Create an analysis environment from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-traces.txt
```

These pinned packages cover all six offline analyses, including RPC trace verification. The commands below use the data already in Git and run without a switch:

```sh
.venv/bin/python scripts/recompute_rpc.py --output results/reanalysis/rpc
.venv/bin/python scripts/recompute_construction.py --output results/reanalysis/construction
.venv/bin/python scripts/verify_inputs.py --output results/reanalysis/inputs
.venv/bin/python scripts/verify_sessions.py --output results/reanalysis/sessions
.venv/bin/python scripts/verify_retries.py --output results/reanalysis/retries
.venv/bin/python scripts/verify_dataplane.py --output results/reanalysis/dataplane
```

| Dataset | Published coverage | Method and record formats |
| --- | --- | --- |
| RPC performance | 1,680 measurements across seven parameter points, six profiles and ten repetition blocks | [Method](rpc-method.md), [columns](data-dictionary.md) |
| Local construction | 270 measurements across 27 conditions and ten repetitions | [Method](construction.md), [columns](construction-data.md) |
| Input validation | 382 fixed cases and 6,984 trials | [Method](inputs.md), [fields](input-data.md) |
| Sessions and streams | 840 trials across 28 configurations and thirty repetitions | [Method](sessions.md), [fields](session-data.md) |
| Batch-write retries | 192 application trials across six profiles and sixteen target-specific scenarios | [Method](retries.md), [fields](retry-data.md) |
| Data-plane cases | 130 cases and 520 PCAP files | [Method](dataplane.md), [fields](dataplane-data.md) |

Each command writes a verification report and reconstructed results to its output directory. Use new output paths for another run. The verifiers check source correspondence, archive inventories and the evidence relevant to their workloads. Statistical analyses compare recomputed values with the saved summaries. Input, session, retry and data-plane checks also reconstruct request or event semantics.

Run the test suites in separate processes:

```sh
for suite in tests tests/traces tests/inputs tests/sessions tests/retries tests/dataplane; do
  .venv/bin/python -m unittest discover -s "$suite" -v || exit 1
done
```

Data-plane archive verification also works with the Python standard library alone. Its [offline instructions](dataplane.md) select the archive and frame tests. The runner tests use gRPC from the analysis environment above.

## Verify the original RPC requests

The seven compressed RPC request archives are [Release assets](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/rpc-traces-v1). Their catalog and per-file inventories are in Git. Download and check all seven parameter points with:

```sh
python3 scripts/fetch_rpc_traces.py --points all
.venv/bin/python scripts/verify_rpc_traces.py --points all
```

The downloader checks compressed sizes and SHA-256 hashes. The verifier reads the compressed records directly, checks all 2,940 measurement and warmup phases, reconstructs complete table state and reproduces the 1,680 measurement rows. Use `--points default` for one parameter point. See [RPC trace archives](rpc-traces.md) for locations, formats and output files.

## Run new experiments

The setup scripts install pinned controller sources and Python dependencies, build the Go drivers and validate fixtures. They select Go 1.26.9. The execution guides describe prerequisites, available modes, source snapshots, outputs and checks.

| Workload | Setup | Default run | Execution guide |
| --- | --- | --- | --- |
| RPC performance | `python3 scripts/bootstrap_rpc.py` | `python3 scripts/run_rpc.py --points all` | [RPC](running-rpc.md) |
| Local construction | `python3 scripts/bootstrap_construction.py` | `python3 scripts/run_construction.py` | [Construction](running-construction.md) |
| Input validation | `python3 scripts/bootstrap_inputs.py` | `python3 scripts/run_inputs.py` | [Inputs](running-inputs.md) |
| Sessions and streams | `python3 scripts/bootstrap_sessions.py` | `python3 scripts/run_sessions.py` | [Sessions](running-sessions.md) |
| Batch-write retries | `python3 scripts/bootstrap_retries.py` | `python3 scripts/run_retries.py` | [Retries](running-retries.md) |
| Data-plane cases | `python3 scripts/bootstrap_dataplane.py` | `sudo python3 scripts/run_dataplane.py` | [Data plane](running-dataplane.md) |

Live execution uses Linux x86_64, Python 3.12, Go and a C compiler. RPC, input, session, retry and data-plane workloads also need p4c and BMv2. Data-plane cases additionally need Mininet and sudo for namespaces and raw Ethernet sockets. Construction runs locally without a target. The recorded environment is WSL2.

New measurements retain the workload and validation rules. Measured timings and periodic sample counts belong to the new run. The execution guides identify which functional counts and invariants are checked.

## Cite a fixed snapshot

Use a GitHub commit permalink when citing the evaluation repository. The RPC request records have the separate `rpc-traces-v1` Release and the SHA-256 catalog in that snapshot. Source code, configuration, tests and documentation use Apache-2.0. Experimental data and derived figures use CC BY 4.0. See [LICENSE.md](../LICENSE.md).
