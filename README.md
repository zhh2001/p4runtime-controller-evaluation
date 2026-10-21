# P4Runtime Controller Evaluation

Data and analysis scripts for comparing P4Runtime controller interfaces. The SDK evaluated here is [p4runtime-go-controller v2.0.1](https://github.com/zhh2001/p4runtime-go-controller/releases/tag/v2.0.1).

The RPC dataset covers performance on BMv2. It contains seven parameter points, six software configurations and ten repetition blocks. Each point has 240 measurement rows, 312 metric summaries and eight paired throughput summaries. The CSV files preserve the recorded values and file hashes.

The local construction dataset has 270 measurements across complete table-entry construction and separate SDK public operations. It includes benchmark outputs, input fixtures, 65 metric summaries, 16 paired summaries and PDF and PNG figures.

## Recompute the statistics

Use Python 3.12:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/recompute_rpc.py --output results/rpc
```

The script checks file hashes and measurement coverage, then recomputes means, medians, sample standard deviations, ranges, coefficients of variation and 95% bootstrap intervals. It also recomputes throughput ratios within matched Go dependency stacks. All 2,240 summary rows must match the saved results. Recomputed CSV files and a verification report are written to `results/rpc/`. BMv2 and the controller toolchains are not needed for this analysis.

Run the analysis tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Data and method

| Material | Location |
| --- | --- |
| Measurement rows and saved summaries | [data/rpc](data/rpc) |
| Parameter points, execution order and SHA-256 hashes | [manifest.json](data/rpc/manifest.json) |
| Workload, software versions and statistics | [RPC method](docs/rpc-method.md) |
| CSV columns and units | [Data dictionary](docs/data-dictionary.md) |
| Statistical reanalysis | [recompute_rpc.py](scripts/recompute_rpc.py) |
| Benchmark setup, execution and validation | [Run the RPC comparison](docs/running-rpc.md) |
| Fixed workloads and dependency versions | [config](config) |
| Drivers and request validation | [experiments/performance](experiments/performance) |
| Original request records and complete readbacks | [RPC trace archives](docs/rpc-traces.md) |
| Local table-entry construction data and outputs | [data/construction](data/construction) |
| Construction workloads, statistics and figures | [Construction method](docs/construction.md) |
| Construction CSV columns and units | [Construction data dictionary](docs/construction-data.md) |
| Construction benchmark setup, execution and validation | [Run the construction comparison](docs/running-construction.md) |
| Offline construction drivers and semantic checks | [experiments/construction](experiments/construction) |

## Recompute construction results

```sh
.venv/bin/python scripts/recompute_construction.py --output results/construction
```

The script checks all 270 benchmark outputs against the saved measurements and recomputes all 81 summary rows. It uses Python and the same pinned NumPy dependency as the RPC reanalysis. See the [construction method](docs/construction.md) for timing scope, result ownership and the archived figures.

## Run new measurements

On Linux x86_64 with Python 3.12, Go, a C compiler, p4c and BMv2 installed:

```sh
python3 scripts/bootstrap_rpc.py
python3 scripts/run_rpc.py --points all --output results/rpc-smoke
```

Setup installs the pinned clients and builds the drivers. The default run validates all seven parameter points with short measurement windows. Use `--mode full` for ten rounds with the published five-second warmup and 30-second measurement windows. See [Run the RPC comparison](docs/running-rpc.md) for prerequisites, outputs and checks.

To run the local construction comparison, use Python 3.12, Go and a C compiler on Linux x86_64:

```sh
python3 scripts/bootstrap_construction.py
python3 scripts/run_construction.py --output results/construction-smoke
```

Setup validates 5,120 constructed entries, result ownership, Go race and vet checks and the semantic oracle. The default run covers all 27 conditions once. Use `--mode full` for ten repetitions and independently checked statistics. See [Run the construction comparison](docs/running-construction.md). This workload does not require a switch or root privileges.

## Verify the original requests

The [RPC trace archives](docs/rpc-traces.md) contain the original request records and full table readbacks for all 2,940 measurement and warmup phases. Download archives by parameter point and verify them directly from their compressed form. The verifier checks request accounting, table state and all 1,680 published measurement rows.

The repository supports statistical reanalysis, request-level verification and new RPC and construction measurements. The remaining experiment groups will be added in subsequent batches.

## License

Source code and documentation use Apache-2.0. Experimental data and derived figures use CC BY 4.0. See [LICENSE.md](LICENSE.md).
