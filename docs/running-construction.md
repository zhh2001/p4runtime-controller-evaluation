# Run the construction comparison

These benchmarks construct `TableEntry` protobufs locally. They use the same inputs, client revisions and timed functions as the [published construction run](construction.md).

## Prerequisites

Use Linux x86_64, CPython 3.12 with `venv` support, Go and a C compiler. Setup selects Go 1.26.9 through `GOTOOLCHAIN`. The first build needs network access or a populated Go module cache. Python wheels and upstream source archives are downloaded during setup unless already available locally.

This experiment uses a P4Info fixture and does not start a switch. It needs neither p4c nor BMv2, Mininet or root privileges.

## Install and validate

From the repository root:

```sh
python3 scripts/bootstrap_construction.py
```

Setup checks SHA-256 hashes for the four upstream source archives in [dependencies.json](../config/dependencies.json). It creates separate Python environments for Finsy, shell and statistical analysis under `.runtime/`. The two benchmark environments retain their original dependencies and protobuf backends. NumPy 1.26.4 is installed in the analysis environment. Python requirements specify wheel hashes.

The preflight builds both Go test binaries and checks 5,120 constructed entries against the input corpus. It checks independent ownership of inputs and results, runs both Go modules with the race detector and `go vet`, and runs nine tests of the semantic oracle. Its logs and sample outputs are saved under `results/construction-setup/`. Setup completes only after these checks pass.

To install Python dependencies from a wheel directory, populate `finsy/`, `shell/` and `analysis/` subdirectories using their respective `config/requirements-*.txt` files, then run:

```sh
python3 scripts/bootstrap_construction.py --wheelhouse /path/to/wheels
```

This option supplies Python wheels. Source archives still come from their configured URLs or `.runtime/downloads/`, and Go uses its module cache or proxy.

## Run

Start with a short run of all 27 conditions:

```sh
python3 scripts/run_construction.py --output results/construction-smoke
```

The default `smoke` mode uses one repetition and a 0.01-second measurement target. It checks execution, output identities, iteration counts, elapsed-time calculations, empty stderr and source hashes. It produces 27 measurement rows without confidence intervals.

Run the full comparison with:

```sh
python3 scripts/run_construction.py --mode full --output results/construction-full
```

Full mode uses the archived plan: ten repetitions, a one-second measurement target and seed 20261019. Each repetition shuffles all twenty entry-construction conditions and seven SDK public operations. Processes run sequentially on the lowest-numbered CPU in the process's available affinity set. That CPU was 0 in the published run. Go uses `GOMAXPROCS=1`; Python keeps GC enabled and performs the original calibration before measuring.

Keep the machine otherwise idle during a full run. The output directory must be new. Rerun setup after changing a benchmark input, binary or pinned dependency. The launcher verifies the setup inventory before starting and records failures in `execution.json`.

## Outputs and analysis

Each output directory contains:

| Path | Contents |
| --- | --- |
| `execution.json` | Setup record, host details and final execution status |
| `run.log`, `analysis.log` | Measurement progress and analysis output |
| `crosscheck.log` | Independent statistical check, in full mode |
| `run/manifest.json` | Plan, shuffled schedule, source hashes, CPU affinity and completion status |
| `run/environment.json` | Host, Go settings, Python versions and protobuf backends |
| `run/source/` | Snapshot of the benchmark, setup inputs and binaries |
| `run/measurements.jsonl` | Process outcomes and measured metrics |
| `run/NNN-*.txt`, `run/NNN-*.json` | Per-process stdout, stderr, command and host state |
| `run/measurement-rounds.csv` | Measurements reconstructed from benchmark outputs |
| `run/round-statistics.csv`, `run/paired-ratios.csv` | 65 metric summaries and 16 paired summaries, in full mode |
| `run/verification.json` | Output audit, source-snapshot check and file hashes |
| `run/summary-crosscheck.json` | Independent check of 567 summary values, in full mode |

Full mode runs the original statistical calculations and then checks the summaries independently with Python's standard library. It uses ten-round percentile bootstrap intervals with 20,000 resamples. See the [method](construction.md#statistics) and [column definitions](construction-data.md).

Analysis can also be repeated separately:

```sh
.runtime/analysis/bin/python experiments/construction/analyze.py results/construction-full/run
.runtime/analysis/bin/python experiments/construction/verify_statistics.py results/construction-full/run
```

For a smoke run, pass `--check-only` to `analyze.py` and omit `verify_statistics.py`.

New outputs stay under `results/`. The launcher keeps them outside the archived `data/` and `figures/` directories. To recompute the published dataset rather than run new measurements, use [recompute_construction.py](../scripts/recompute_construction.py).

## Source correspondence

[construction-source-provenance.json](../config/construction-source-provenance.json) records original and exported hashes for 24 source, plan and fixture files. Eighteen files preserve their original bytes, including all Go construction code, the Python driver, the input generator and the semantic oracle. The two module files use repository-local dependency paths. Preparation, execution, test-sample selection and analysis have documented path and packaging adaptations.

The scheduler and measurement functions are unchanged. Analysis uses the source snapshot saved with the original statistics. Its summary calculations are unchanged; manuscript report writing and figure rendering are omitted from this execution path. Additional output checks verify complete benchmark identities and empty stderr. The inventory also records hashes for the unchanged scheduler, measurement and statistical functions.

Git attributes preserve benchmark file bytes and LF line endings in the scripts and configuration used by setup, including checkouts with automatic line-ending conversion enabled.
