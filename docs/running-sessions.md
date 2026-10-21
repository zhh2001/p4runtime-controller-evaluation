# Run the session and stream comparison

The E3 runners compare SDK v2.0.1 and Finsy v0.30.0 using the [archived plan](sessions.md). They cover native session recovery, mastership, cancellation and close, and slow consumers.

## Prerequisites and setup

Use Linux x86_64, CPython 3.12 with `venv` support, Go, a C compiler, `p4c-bm2-ss` and `simple_switch_grpc` on `PATH`. Setup selects Go 1.26.9. Source archives, Python wheels and Go modules need network access or populated local caches on the first setup.

```sh
python3 scripts/bootstrap_sessions.py
```

Setup verifies the four source archives in [dependencies.json](../config/dependencies.json). These experiments use the pinned SDK and Finsy sources. Finsy and NumPy run in separate Python environments under `.runtime/`. The SDK's 148 Go and module files must match the archived session environment.

Setup compiles the P4 program and compares its P4Info declarations with the archive. It builds ordinary and race-enabled binaries for the session and stream Go modules, runs `go vet` for both modules, and runs the 15 original evidence and service-close tests. Its record contains compiler, target and Python versions, dependency inventories and source, fixture and binary hashes. Logs are written to `results/session-setup/`.

To supply Python wheels locally, populate `finsy/` and `analysis/` subdirectories from `config/requirements-finsy.txt` and `config/requirements-analysis.txt`, then use:

```sh
python3 scripts/bootstrap_sessions.py --wheelhouse /path/to/wheels
```

The option supplies Python wheels. Source archives still come from their configured URLs or `.runtime/downloads/`, and Go uses its module cache or proxy.

## Execute

Run the short validation first:

```sh
python3 scripts/run_sessions.py --output results/sessions-smoke
```

| Group | Smoke trials | Race trials | Full trials |
| --- | ---: | ---: | ---: |
| Recovery | 6 | 3 | 180 |
| Mastership | 2 | 1 | 60 |
| Cancellation and close | 8 | 6 | 240 |
| Slow consumers | 12 | 6 | 360 |
| Total | 28 | 16 | 840 |

The default `smoke` mode runs one repetition of every configuration. Race mode runs the 16 SDK configurations once with the Go race detector enabled. Full mode runs all 28 configurations for 30 repetitions and computes statistical summaries.

```sh
python3 scripts/run_sessions.py --mode race --output results/sessions-race
python3 scripts/run_sessions.py --mode full --output results/sessions-full
```

Select groups with `--groups`:

```sh
python3 scripts/run_sessions.py --groups cancellation consumers --output results/stream-smoke
python3 scripts/run_sessions.py --mode full --groups recovery --output results/recovery-full
```

Groups and trials run sequentially. The default group order follows the plan. An explicit group list uses the supplied order. Configurations are shuffled within each repetition with the original seed. A selected-group run uses the same per-group schedule as the complete run.

Recovery and mastership start owned BMv2 processes on loopback with dynamic ports, device ID 1 and CPU port 255, without network interfaces. The two other groups use controlled gRPC services. Mininet and root privileges are not needed. Keep the machine otherwise idle when collecting new timing results.

The clients retain their default reconnect policies. The application restores a missing pipeline after target restart. All recovery trials retain the 2.1-second settling period and 60-second observation window. Consumer runs retain 300 messages at 500 messages per second and the original queue capacities. [The method](sessions.md) explains these conditions and the timing boundaries.

Cancellation runs allow one second for each backpressure probe before selecting a pending send. The subsequent pending-state and cancellation checks remain in place. The original 200-millisecond probe selected a send that completed during the next check in a port-validation run. This longer probe changes preparation of the controlled cancellation condition. Payloads, paused reads and cancellation outcomes use the original checks.

Output directories must be new. Sources, binaries, fixtures and pinned dependencies are checked before and after execution. Rerun setup after changing these inputs. The launcher isolates the selected normal or race binaries and stops its child process group if execution fails or is interrupted.

## Outputs and analysis

| Path under the output directory | Contents |
| --- | --- |
| `execution.json` | Mode, selected groups, setup record, host details and final status |
| `run.log`, `analysis.log` | Execution progress and verification output |
| `run/state.json` | Current group and trial, completed count and overall completion status |
| `run/<group>/manifest.json` | Plan, selected profiles, schedule, source hashes and driver hash |
| `run/<group>/source/` | Experiment and analysis sources, setup inputs, fixtures and binaries |
| `run/<group>/observations.jsonl`, `rpc.jsonl` | Per-trial outcomes and complete event records |
| `run/<group>/*.stderr` | Driver stderr, one file per trial |
| `run/<group>/*.driver.jsonl` | Raw driver replies, including output from an interrupted trial |
| `run/<group>/*-target/`, `target-processes.json` | Owned BMv2 logs and process history, where applicable |
| `run/<group>/verification.json` | Primary evidence checks |
| `run/extra-verification.json` | Raw Write and observed-backup phase checks |
| `run/analysis.json` | Combined coverage, source and semantic verification |
| `run/statistics.csv`, `summary.json` | Numerical summaries and configuration outcomes, in full mode |
| `run/statistics-verification.json` | Independent NumPy recomputation, when numerical summaries are present |

Verification checks the complete schedule, trial and event identities, source snapshots, driver log coverage, process exit records and request or target-state semantics. It uses the same primary evidence functions and independent JSON/base64/integer decoder as the public archive. Recovery records retain right-censored outcomes if the observation window expires.

Smoke and race runs check functional behavior without bootstrap intervals. Full runs use the original descriptive statistics and median bootstrap calculations. With all four groups and all recovery trials successful, this produces 62 statistical rows and checks 496 summary values independently. A full cancellation-only run produces its eight configuration outcomes and a header-only numerical CSV. Consumer message counts and recovery times are recomputed from the new records.

Repeat analysis of a completed run with:

```sh
.runtime/finsy/bin/python scripts/analyze_sessions.py results/sessions-full/run
```

New outputs remain under `results/` and outside the published `data/` and `figures/` directories. To verify the original 840 archived trials without starting any processes, use [verify_sessions.py](../scripts/verify_sessions.py).

## Source correspondence

[session-runner-provenance.json](../config/session-runner-provenance.json) records original and exported hashes for 22 execution and analysis files. The four trial functions, controlled service, Finsy drivers, Go source and original service-close tests preserve their frozen implementations. Go module replacements use the repository-local SDK source.

Packaging changes adapt interpreter and output paths, setup checks and source snapshots. Target and driver startup failures close their owned processes. Drivers also save each raw reply. The cancellation helper uses a one-second backpressure probe, recorded in the group manifest. The four trial functions and their outcome checks are unchanged. The scheduler keeps the original configuration generation, per-group seeds and trial calls. Analysis uses the archived evidence functions. Statistical and raw-event entry points accept selected groups, including runs with no numerical summaries. The NumPy check also handles a recovery metric with only one uncensored sample using the primary calculator's zero sample deviation.

Git attributes preserve the source and fixture bytes in checkouts with automatic line-ending conversion enabled.
