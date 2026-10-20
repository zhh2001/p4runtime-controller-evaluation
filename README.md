# P4Runtime Controller Evaluation

Data and analysis scripts for comparing P4Runtime controller interfaces. The SDK evaluated here is [p4runtime-go-controller v2.0.1](https://github.com/zhh2001/p4runtime-go-controller/releases/tag/v2.0.1).

The RPC dataset covers performance on BMv2. It contains seven parameter points, six software configurations and ten repetition blocks. Each point has 240 measurement rows, 312 metric summaries and eight paired throughput summaries. The CSV files preserve the recorded values and file hashes.

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

## Run new measurements

On Linux x86_64 with Python 3.12, Go, a C compiler, p4c and BMv2 installed:

```sh
python3 scripts/bootstrap_rpc.py
python3 scripts/run_rpc.py --points all --output results/rpc-smoke
```

Setup installs the pinned clients and builds the drivers. The default run validates all seven parameter points with short measurement windows. Use `--mode full` for ten rounds with the published five-second warmup and 30-second measurement windows. See [Run the RPC comparison](docs/running-rpc.md) for prerequisites, outputs and checks.

The repository supports statistical reanalysis and new RPC measurements. The original per-request traces and remaining experiment groups will be added in subsequent batches.

## License

Source code and documentation use Apache-2.0. Experimental data and derived figures use CC BY 4.0. See [LICENSE.md](LICENSE.md).
