# Run the batch-write retry comparison

The E4 runners execute the [archived application policy and cases](retries.md) against a controlled service and BMv2. They retain the six software profiles, original operation indexes, per-call status handling and final-state checks.

## Setup

Use Linux x86_64, CPython 3.12 with `venv` support, Go, a C compiler, `p4c-bm2-ss` and `simple_switch_grpc` on `PATH`. Setup selects Go 1.26.9. The first setup needs network access or populated source, wheel and Go module caches.

```sh
python3 scripts/bootstrap_retries.py
```

Setup verifies the source archives in [dependencies.json](../config/dependencies.json) and compares all four client revisions with the original E4 manifests. The SDK's Go and module files must match the archived inventory. Finsy and shell use separate Python environments under `.runtime/`, with their recorded protobuf backends.

Setup regenerates the semantic case corpus and compares its bytes with the archive. It compiles the P4 fixture, checks its declarations against the archived P4Info, runs `go vet` for both Go modules, and builds ordinary and race-enabled drivers. It also runs the E4 policy, archive and runner tests. Logs are written to `results/retry-setup/`. `.runtime/retry-setup.json` records source, fixture and binary hashes, compiler and target versions, and Python environment signatures.

To provide Python wheels locally, populate `finsy/` and `shell/` subdirectories using their respective files in `config/requirements-*.txt`:

```sh
python3 scripts/bootstrap_retries.py --wheelhouse /path/to/wheels
```

Source archives still use their configured URLs or `.runtime/downloads/`. Go uses its module cache or proxy.

## Execute

Start with the short validation:

```sh
python3 scripts/run_retries.py --output results/retries-smoke
```

| Target | Smoke trials | Race trials | Full trials |
| --- | ---: | ---: | ---: |
| Controlled service | 18 | 20 | 120 |
| BMv2 | 12 | 12 | 72 |
| Total | 30 | 32 | 192 |

Smoke mode runs one repetition of three controlled cases and two BMv2 cases in all six profiles. The selected cases cover rebased retry indexes, incomplete status, uncertain RPC outcome, mixed conflicts and all-duplicate batches. Race mode runs every case once through SDK and legacy Go profiles with the race detector. Full mode runs all sixteen target-specific cases in all six profiles for two repetitions.

```sh
python3 scripts/run_retries.py --mode race --output results/retries-race
python3 scripts/run_retries.py --mode full --output results/retries-full
```

Select targets with `--targets`:

```sh
python3 scripts/run_retries.py --targets controlled --output results/retries-controlled
python3 scripts/run_retries.py --mode full --targets bmv2 --output results/retries-bmv2
```

Targets run sequentially in the selected order. Within a target, trials follow repetition, profile and case order. Each application starts in a separate process. BMv2 reinstalls the pipeline and seeds before each trial. The target uses dynamic loopback ports, device ID 1 and CPU port 255, without network interfaces. Mininet and root privileges are not needed.

The application retains the four-attempt bound, six-second driver watchdog and recorded RPC deadline behavior. Expected shell parser watchdog terminations remain trial outcomes and are checked against the cases. Successful operations, permanent failures and uncertain RPC outcomes follow the [application retry policy](retries.md#retry-policy). The SDK itself does not retry writes automatically.

Output directories must be new and outside the published `data/` and `figures/` directories. Setup inputs, pinned sources, interpreter signatures and generated files are checked before and after execution. Rerun setup after changing these inputs. The launcher stops its child process group if execution fails or is interrupted.

## Outputs and verification

| Path under the output directory | Contents |
| --- | --- |
| `execution.json` | Selected mode and targets, host and setup record, final status |
| `controlled.log`, `bmv2.log`, `analysis.log` | Selected target progress and verification output |
| `run/state.json` | Selected targets, current target, completed trial count and completion status |
| `run/<target>/manifest.json` | Profiles, cases, repetitions, plan, versions and snapshot hashes |
| `run/<target>/source/` | Experiment and analysis sources, fixtures, setup inputs and binaries |
| `run/<target>/observations.jsonl`, `rpc.jsonl` | Application steps, binary requests, outcomes and state snapshots |
| `run/<target>/<token>/` | Driver command, raw stdout and stderr, with `|` replaced by `__` in directory names |
| `run/bmv2/target/`, `target-processes.json` | BMv2 logs and process start, stop and exit history |
| `run/<target>/artifact-hashes.json` | Recorded execution file hashes |
| `run/<target>/analysis.json`, `independent-verification.json` | Primary and independent retry/state reconstruction |
| `run/counts.csv`, `profile-outcomes.csv`, `trials.csv` | Per-target counts, selected-profile outcomes and per-trial summaries |
| `run/analysis.json` | Combined counts and source and semantic verification status |

The verifier checks exact case coverage and order, RPC sequences, driver commands and output, snapshot and artifact hashes, binary/JSON correspondence, original-index retry subsets, and final target state. It also checks target exit records, removed IPC files and driver race reports. The primary protobuf decoder and unchanged standard-library wire decoder reconstruct the results independently. Counts include retries and exclude setup calls and independent observer Reads.

Repeat analysis of a completed run with:

```sh
.runtime/finsy/bin/python scripts/analyze_retries.py results/retries-full/run
```

These are functional trials. Their summaries report outcomes and operation counts. To verify the original 192 archived trials without starting a target, use [verify_retries.py](../scripts/verify_retries.py).

## Source correspondence

[retry-runner-provenance.json](../config/retry-runner-provenance.json) records original and exported hashes for twelve execution files. The controlled endpoint, Python driver, formatted Go source and Go checksum files are unchanged. Go module replacements point to the repository-local pinned sources. The `wire`, `snapshot` and `trial` functions retain their original bodies, including retry decisions and attempt limits.

Packaging adapts interpreter and output paths, setup checks and source snapshots. Target and observer startup failures close owned resources. Driver startup and shutdown failures close logs and reap the process. The public launcher preserves the original smoke, race and full selections. Analysis reuses the archived retry model, status guard and both decoders. Snapshots include the case corpus at the path expected by the unchanged wire decoder. Git attributes preserve source bytes when automatic line-ending conversion is enabled.
