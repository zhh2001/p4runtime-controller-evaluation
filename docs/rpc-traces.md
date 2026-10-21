# Original RPC request records

The `rpc-traces-v1` dataset contains the original request records and complete table readbacks for all seven RPC parameter points. Each point has 420 phases across six profiles and ten repetition blocks. Of those phases, 240 are measurements and 180 are warmups. The warmups are included because their writes establish the table state used by the following phases.

The compressed archives are distributed as [GitHub Release assets](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/rpc-traces-v1). The repository stores the [archive catalog](../data/rpc/traces/manifest.json), per-file inventories, P4Info and verification scripts.

## Download

The downloader uses only Python's standard library. Download one parameter point:

```sh
python3 scripts/fetch_rpc_traces.py --points default
```

Use `--points all` for all seven archives, or select several names such as `--points n256 c16`. Files are saved under `results/rpc-traces/`. Use `--directory /path/to/archives` to choose another location.

The catalog lists compressed sizes and SHA-256 hashes. The downloader checks both before accepting a file and checks existing downloads before reusing them.

## Verify

Use CPython 3.12 on Linux x86_64:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: \
  -r config/requirements-traces.txt
.venv/bin/python scripts/verify_rpc_traces.py --points default
```

Verification reads members directly from the compressed archive. It does not extract the full JSON dataset. BMv2, p4c and Go are not needed. The requirements pin NumPy and the Finsy package that supplies the P4Runtime protobuf classes.

For the complete dataset:

```sh
python3 scripts/fetch_rpc_traces.py --points all
.venv/bin/python scripts/verify_rpc_traces.py --points all
.venv/bin/python scripts/recompute_rpc.py --output results/rpc
```

The trace verifier checks:

- Archive and member hashes, phase coverage and shuffled execution order.
- Driver wire samples, request counts, per-worker sequencing and concurrent RPC counts.
- Successful write updates and read results, including keys and action parameters.
- Full table state after each of the 2,940 phases.
- All 1,680 published measurement rows, including request latency, construction time, call time, throughput and sampled CPU and memory usage.
- Driver exits and the owned switch's termination record.

The separate statistical reanalysis then checks the 2,184 metric summaries and 56 paired throughput summaries from those measurement rows.

The trace report is written to `results/rpc-trace-verification.json`. Use `--output` to select another file. The `--directory` option selects the archive directory. Request audits use Python assertions, so run the verifier without `-O`.

## Archive layout

Each `rpc-traces-v1-<point>.tar.gz` contains a directory named after the parameter point.

| Member | Content |
| --- | --- |
| `run.json` | Workload parameters, profile order, durations and protocol revision |
| `drivers.json` | Per-profile driver process exits |
| `target-processes.json` | Owned switch command and process lifetime |
| `phase-0.json` through `phase-419.json` | Request records, resource samples and validation metadata |
| `state-0.pb.gz` through `state-419.pb.gz` | Serialized `p4.v1.ReadResponse` messages from full table readback |

The phase, readback and process files preserve the original bytes. `run.json` selects the experiment fields from the original run manifest. It omits local executable locations and the broad source-file inventory. Each point's inventory records the original manifest hash and the exact selected field names. The exported source correspondence is documented in [source-provenance.json](../config/source-provenance.json).

Readback files retain their original gzip encoding. Each phase also records hashes of both the compressed readback and its uncompressed protobuf bytes. The included [P4Info](../data/rpc/traces/control.p4info.txt) defines the table, match field and action parameter IDs.

A measurement phase maps to `measurement-rounds.csv` through its `profile`, `repetition` and `operation`. Phase indices describe execution order, including warmups. See the [data dictionary](data-dictionary.md) for field definitions.

## Packaging and tests

To rebuild the archives from the original run directories:

```sh
python3 scripts/package_rpc_traces.py \
  --source-root /path/to/performance \
  --output /path/to/new-archives \
  --index-output /path/to/new-index
```

The source directory contains the `source_run` directories named in the measurement manifest. Output directories must be new. Tar member metadata and gzip timestamps are fixed. Packaging preserves source file bytes and produces a catalog with archive and member hashes.

Run the archive, download, decoding and metric tests with:

```sh
.venv/bin/python -m unittest discover -s tests/traces -v
```

The archives and inventories use CC BY 4.0. Attribute the data to Henghua Zhang, Tiancheng Yang and Jue Chen and link to this repository. Source code uses Apache-2.0. See [LICENSE.md](../LICENSE.md).
