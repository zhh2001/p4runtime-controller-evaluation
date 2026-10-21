# Local construction benchmarks

Run installation and measurement from the repository root:

```sh
python3 scripts/bootstrap_construction.py
python3 scripts/run_construction.py --output results/construction-smoke
python3 scripts/run_construction.py --mode full --output results/construction-full
```

See [Run the construction comparison](../../docs/running-construction.md) for dependencies, preflight checks and outputs. The [method](../../docs/construction.md) describes workloads, timing and ownership. Original and exported source hashes are in [construction-source-provenance.json](../../config/construction-source-provenance.json).

`go-sdk/` and `go-legacy/` are separate modules with pinned dependencies. Their benchmark files duplicate the shared Go sources in this directory, and setup checks their equality. `python_driver.py` provides the Finsy and shell paths. `oracle.py` checks complete protobuf results against the corpus in `fixtures/`.

`prepare.py` runs semantics, ownership, race and vet checks and builds the Go benchmark binaries. `run.py` schedules processes. `analyze.py` reconstructs measurements and calculates summaries. `verify_statistics.py` independently checks those summaries.

Generated binaries, environments and run outputs are local files. The published dataset and figures are under [data/construction](../../data/construction) and [figures/construction](../../figures/construction).
