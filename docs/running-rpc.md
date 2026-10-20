# Run the RPC comparison

The runner covers the seven parameter points in [the RPC method](rpc-method.md). Each point runs `sdk`, `raw-current`, `legacy`, `raw-legacy`, `finsy` and `shell` in a seeded order. It starts its own BMv2 process on unused loopback ports and stops that process when the run ends. No network interfaces or root privileges are needed.

## Setup

Use Linux x86_64 with CPython 3.12, Go, a C compiler, `p4c-bm2-ss` and `simple_switch_grpc` on `PATH`. The published measurements used Go 1.26.9, Python 3.12.3, p4c 1.2.5.10 (`8b6de3c57`) and BMv2 1.15.0 (`2bdd0b7b`). The setup script selects Go 1.26.9 and records the installed compiler and switch versions.

From the repository:

```sh
python3 scripts/bootstrap_rpc.py
```

Setup downloads four upstream source archives at fixed commits, checks their SHA-256 hashes, and installs separate Python environments with hashed wheel requirements. It compiles the P4 fixture, builds ordinary and race-enabled Go drivers, runs `go vet`, and runs the Go and Python driver tests. A successful setup writes `.runtime/setup.json` and `results/setup/bootstrap.log`.

The source archives and Python environments stay in `.runtime/`. Generated P4 artifacts and executables also stay outside version control. The source archive locks are in [dependencies.json](../config/dependencies.json), and the two Go modules retain their own `go.mod` and `go.sum`.

For an existing wheel cache, use `--wheelhouse /path/to/wheelhouse`, with wheels under `finsy/` and `shell/`. This option replaces Python package downloads. The four source archives and Go modules still need network access unless already cached.

The P4 compilation removes `Table.initial_default_action` from the generated P4Info so both pinned Go protobuf versions can parse it. The BMv2 JSON retains the compiled behavior. `fixtures/compatibility.json` records this transformation and the generated file hashes.

## Short validation

```sh
python3 scripts/run_rpc.py --points all --output results/rpc-smoke
```

The default mode uses two repetition blocks, a 0.05-second warmup and a 0.15-second measurement window. INSERT still inserts all N entries. Each selected point retains its specified N, B and C. A seven-point short run produces 336 measurement rows and checks 588 complete table states.

Use `--points default` for one point, or provide several names such as `--points n256 c16`. The output directory must be new. The launcher can also be called by absolute path from another working directory.

## Full measurement

```sh
python3 scripts/run_rpc.py --mode full --points all --output results/rpc-full
```

Full mode uses the published ten repetition blocks, five-second warmups and 30-second measurement windows. Across all seven points, the timed windows alone take about 12.25 hours. INSERT, process startup, readback, serialization and analysis add to this.

Run on an otherwise idle host. The original environment was WSL2 with an 8 GiB memory limit and 2 GiB swap. Setup and execution records include software versions, CPU affinity and Linux memory information so results can be interpreted with their host configuration.

The launcher uses ordinary Go binaries for measurements. It runs parameter points sequentially and checks dependency sources, configuration files and compiled artifacts before starting. Run setup again after changing a setup input.

## Outputs and checks

Each point has:

| File | Content |
| --- | --- |
| `raw-phase-*.json` | Request records saved before validation |
| `phase-*.json` | Validated timing, outcome and resource records |
| `state-*.pb.gz` | Complete table readbacks |
| `manifest.json`, `source/` | Parameters, execution order and source snapshot |
| `verification.json` | Request counts, state checks and process exit checks |
| `measurement-rounds.csv` | Per-round values in the published dataset's schema |
| `round-statistics.csv`, `paired-ratios.csv` | Across-round statistics and matched Go ratios |
| `summary-verification.json`, `analysis-source/` | Summary counts and analysis provenance |

The enclosing directory contains `execution.json` and per-point logs. The analysis checks request counts, per-worker ordering, concurrency, successful updates, read results, complete table state and process cleanup. For a validation failure, the original request records remain available in the output directory.

Statistics use the same calculation functions as the formal runs, including the 20,000 bootstrap samples. Short-run summaries describe the validation run. The published ten-round measurements remain under `data/rpc/`.

## Source correspondence

[source-provenance.json](../config/source-provenance.json) records the SHA-256 hashes of the frozen sources from all seven formal runs and their exported counterparts. Request drivers, timing loops, request audits and workload orchestration are unchanged. Export changes cover local paths, source snapshots and the P4 compiler's working directory. The target helper contains the original status, trace and process-management functions with a separate IPC filename prefix.

The statistics module retains the original phase metrics and across-round calculation functions. Its entry point groups the new run's measurements and writes the same CSV fields. The launcher and dependency installer are new entry points for this repository.

## Additional validation

Run the dataset tests and statistical reanalysis using the environment described in the [README](../README.md#recompute-the-statistics). Setup runs the driver tests, which can also be repeated with:

```sh
.runtime/finsy/bin/python -m unittest discover -s experiments/performance/tests -v
```

The controlled RPC target checks request contents and injected failures without BMv2:

```sh
.runtime/finsy/bin/python experiments/performance/run.py \
  --oracle --inject-failure --smoke --output results/rpc-oracle
.runtime/finsy/bin/python experiments/performance/analyze.py results/rpc-oracle
```

To validate the same workload with Go's race detector, set `P4PERF_SDK_BINARY` and `P4PERF_LEGACY_BINARY` to the absolute paths of `experiments/performance/bin/go-sdk-race` and `go-legacy-race` for the direct `run.py` command. The timed launcher clears these overrides.
