# Run the input-validation comparison

The E1 runners exercise the same 382 fixed inputs and six client profiles as the [input-validation archive](inputs.md). They send requests to a controlled P4Runtime service or an owned BMv2 process, record the outcomes and target state, and verify the results.

## Prerequisites and setup

Use Linux x86_64, CPython 3.12 with `venv` support, Go, a C compiler, `p4c-bm2-ss` and `simple_switch_grpc` on `PATH`. Setup selects Go 1.26.9. The first setup needs network access or cached source archives, Python wheels and Go modules.

From the repository root:

```sh
python3 scripts/bootstrap_inputs.py
```

Setup verifies the four upstream source archives in [dependencies.json](../config/dependencies.json), installs separate Finsy and shell Python environments, and builds six Go modules into twelve normal and race-enabled binaries. It runs `go vet` for each module and 34 oracle and endpoint tests.

It also compiles three P4 programs. The generated corpora, P4 sources and schemas must match the archived inputs byte for byte, and generated P4Info must match the archived protobuf declarations. The setup record includes compiler and target versions, dependency inventories, Python protobuf backends and input and binary hashes. Logs are saved under `results/input-setup/`.

The validation run used p4c 1.2.5.10 (8b6de3c57) and BMv2 1.15.0-2bdd0b7b. Setup checks declarations produced by the installed compiler. Target behavior is recorded by each run.

To supply Python wheels locally, populate `finsy/` and `shell/` subdirectories from their respective `config/requirements-*.txt` files, then use:

```sh
python3 scripts/bootstrap_inputs.py --wheelhouse /path/to/wheels
```

Source archives are read from their configured URLs or `.runtime/downloads/`. Go uses its module cache or proxy.

## Execute

Start with all three groups on both targets:

```sh
python3 scripts/run_inputs.py --output results/inputs-smoke
```

The default `smoke` mode selects shorter corpora. Full mode runs two repetitions of every selected archived input with all six profiles. Race mode uses the SDK and legacy Go profiles with the race detector enabled.

| Group and target | Smoke trials | Full trials | Race trials |
| --- | ---: | ---: | ---: |
| Baseline, controlled | 96 | 336 | 16 |
| Baseline, BMv2 | 96 | 336 | 16 |
| Boundaries, controlled | 396 | 3,432 | 132 |
| Boundaries, BMv2 | 120 | 1,248 | 40 |
| Constraints, controlled | 132 | 816 | 44 |
| Constraints, BMv2 | 132 | 816 | 44 |
| Total | 972 | 6,984 | 292 |

Baseline uses two repetitions in every mode. Boundaries and constraints use one repetition in smoke and race modes, and two in full mode. Smoke and full modes include SDK, legacy, Finsy, shell, raw-current and raw-legacy profiles. The [method](inputs.md#corpus-and-runs) explains the BMv2 boundary subset and the profiles' API coverage.

```sh
python3 scripts/run_inputs.py --mode full --output results/inputs-full
python3 scripts/run_inputs.py --mode race --output results/inputs-race
```

Select one group or target when needed:

```sh
python3 scripts/run_inputs.py --group boundaries --target controlled --output results/boundaries-smoke
python3 scripts/run_inputs.py --mode full --group constraints --target bmv2 --output results/constraints-full
```

The launcher runs groups and targets sequentially. Services bind to loopback addresses with dynamically selected ports. BMv2 uses device ID 1 and CPU port 255, without network interfaces. These tests need neither Mininet nor root privileges. The runners stop the target processes they start.

Each output directory must be new. Rerun setup after changing experiment sources, fixtures, binaries or pinned dependencies. The launcher verifies setup hashes before execution and after completion. New outputs remain separate from the archived `data/` and `figures/` directories.

## Outputs and verification

The output root contains `execution.json` with setup details, selected groups and targets, completion status and per-run verification summaries. Each group and target has separate execution and analysis logs. Its `<group>/<target>/` directory contains:

| Path | Contents |
| --- | --- |
| `manifest.json` | Selected cases, profiles, repetitions, versions, source hashes and completion status |
| `source/` | Experiment sources, archived fixtures and selected group's generated fixtures and binaries |
| `observations.jsonl` | Per-trial outcomes and saved state |
| `rpc.jsonl` | Recorded requests and associated RPC results |
| `logs/` or `round-<n>/` | Driver commands and process output |
| `target/`, `target-processes.json` | BMv2 logs and owned-process history, for BMv2 runs |
| `case-results.csv`, `counts.csv` | Verified classifications and counts |
| `analysis.json` | Coverage, semantic checks and output hashes |
| `verification.json` | Primary analyzer report, for boundaries and constraints |
| `findings.json` | Preserved target findings, for constraints |

Analysis checks complete case/profile/repetition coverage, request association, source snapshots, numerical or typed values and target state. It also checks driver stderr for Go race reports. Boundaries and constraints use their original analyzers followed by independent JSON, base64 and integer decoders. Baseline uses the same semantic verifier as the public archive.

Verification preserves the known BMv2 outcomes documented in the [method](inputs.md), including explicit full-range wildcard readbacks and accepted constraint violations. A successful verification means recorded outcomes and state satisfy these checks. The trial counts above describe functional validation and do not measure throughput or latency.

Repeat analysis of a completed run with:

```sh
.runtime/finsy/bin/python scripts/analyze_inputs.py --group boundaries results/inputs-full/boundaries/bmv2
```

To verify the published records without starting a target, use [verify_inputs.py](../scripts/verify_inputs.py).

## Source correspondence

[input-runner-provenance.json](../config/input-runner-provenance.json) records original and exported hashes for 49 source, plan and test files. Go driver code, Python adapters, numerical models, controlled endpoints, semantic oracles and original test bodies retain their frozen implementations. Module replacements point to the repository-local pinned dependencies.

The baseline launcher selects the 28 E1 cases from the original combined workload, starts the original controlled endpoint in process, and uses the original BMv2 child invocation and comparison functions. The extended runners retain their case loops and driver methods. Packaging changes adapt paths, setup checks, output selection and source snapshots. P4 compilation uses relative filenames so repository paths may contain spaces. The shared BMv2 launcher also cleans up its process if startup fails.

Each source snapshot includes fixture aliases under the original experiment directory names so the unchanged analyzers can read their expected paths. These aliases are included in the snapshot hash inventory. Git attributes preserve source and fixture bytes when automatic line-ending conversion is enabled.
