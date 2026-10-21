# P4Runtime Controller Evaluation

Data and analysis scripts for comparing P4Runtime controller interfaces. The SDK evaluated here is [p4runtime-go-controller v2.0.1](https://github.com/zhh2001/p4runtime-go-controller/releases/tag/v2.0.1).

The RPC dataset covers performance on BMv2. It contains seven parameter points, six software configurations and ten repetition blocks. Each point has 240 measurement rows, 312 metric summaries and eight paired throughput summaries. The CSV files preserve the recorded values and file hashes.

The local construction dataset has 270 measurements across complete table-entry construction and separate SDK public operations. It includes benchmark outputs, input fixtures, 65 metric summaries, 16 paired summaries and PDF and PNG figures.

The input-validation dataset contains 382 fixed cases and 6,984 trials on a controlled service and BMv2. It includes complete selected requests and state snapshots, original extended-run classifications, and offline semantic verification.

The session and stream dataset contains 840 trials across 28 configurations and 30 repetitions. It includes recovery and mastership on BMv2, controlled cancellation and close scenarios, slow-consumer events, 62 statistical summaries, and PDF and PNG figures.

The batch-retry dataset contains 192 application trials across six profiles, sixteen target-specific scenarios and two repetitions. It includes original-index retry decisions, binary requests, driver outputs and complete state snapshots, checked by two independent decoders.

The data-plane dataset contains 130 Mininet/BMv2 cases, all 520 PCAP files and 2,420 case files. It covers reactive forwarding, batched table updates, counter collection, application recovery and p4ctl, with 27,170 received frames and independent offline verification.

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
| Input-validation cases, request records and results | [data/inputs](data/inputs) |
| Input workloads, outcome counts and semantic verification | [Input method](docs/inputs.md) |
| Input fields and outcome names | [Input data dictionary](docs/input-data.md) |
| Input-validation setup, execution and checks | [Run the input-validation comparison](docs/running-inputs.md) |
| Live input drivers, controlled services and oracles | [experiments/inputs](experiments/inputs) |
| Session recovery, cancellation, close and slow consumers | [Session and stream method](docs/sessions.md) |
| Session event fields and statistical units | [Session data dictionary](docs/session-data.md) |
| Session observations, events and statistical results | [data/sessions](data/sessions) |
| Session setup, execution and checks | [Run the session and stream comparison](docs/running-sessions.md) |
| Session drivers, controlled service and scheduler | [experiments/sessions](experiments/sessions) |
| Batch-write retry policy, scenarios and outcomes | [Batch-retry method](docs/retries.md) |
| Retry observations, requests, driver outputs and results | [data/retries](data/retries) |
| Retry fields and summary counts | [Batch-retry data dictionary](docs/retry-data.md) |
| Retry setup, execution and verification | [Run the batch-write retry comparison](docs/running-retries.md) |
| Retry drivers, controlled service and scheduler | [experiments/retries](experiments/retries) |
| Data-plane traffic, counter, recovery and CLI cases | [Data-plane method](docs/dataplane.md) |
| Captures, controller/host records and case results | [data/dataplane](data/dataplane) |
| Capture format, event fields and counts | [Data-plane data dictionary](docs/dataplane-data.md) |

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

To run the input-validation comparison with p4c and BMv2 installed:

```sh
python3 scripts/bootstrap_inputs.py
python3 scripts/run_inputs.py --output results/inputs-smoke
```

Setup compiles three P4 programs, checks all 382 inputs and generated declarations against the archive, and builds normal and race-enabled Go drivers. The default run executes 972 trials across the three groups and two targets. Use `--mode full` for all 6,984 trials or `--mode race` for 292 Go trials. Each run verifies coverage, requests and target state. See [Run the input-validation comparison](docs/running-inputs.md).

To run the session and stream comparison:

```sh
python3 scripts/bootstrap_sessions.py
python3 scripts/run_sessions.py --output results/sessions-smoke
```

Setup checks the pinned SDK, compiles the shared P4 fixture and builds both Go driver modules. The default run checks all 28 configurations once. Use `--mode race` for the 16 SDK configurations or `--mode full` for 840 trials and independently checked statistics. `--groups` selects recovery, mastership, cancellation or consumers. See [Run the session and stream comparison](docs/running-sessions.md).

To run the batch-write retry comparison:

```sh
python3 scripts/bootstrap_retries.py
python3 scripts/run_retries.py --output results/retries-smoke
```

Setup checks the pinned clients and case corpus, compiles the P4 fixture and builds normal and race-enabled Go drivers. The default run checks 30 application trials across both targets. Use `--mode race` for 32 Go trials or `--mode full` for all 192 trials. Every run uses both decoders to reconstruct retry subsets and target state. See [Run the batch-write retry comparison](docs/running-retries.md).

## Verify the original requests

The [RPC trace archives](docs/rpc-traces.md) contain the original request records and full table readbacks for all 2,940 measurement and warmup phases. Download archives by parameter point and verify them directly from their compressed form. The verifier checks request accounting, table state and all 1,680 published measurement rows.

## Verify input-validation results

```sh
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-inputs.txt
.venv/bin/python scripts/verify_inputs.py --output results/inputs
.venv/bin/python -m unittest discover -s tests/inputs -v
```

The verifier checks all 6,984 trials, their associated requests and repeated outcomes. It compares numerical and typed values, verifies target state, and reproduces the saved extended-run CSV files and findings. See the [input method](docs/inputs.md) for the three corpora, selected runs and original-source correspondence. This analysis uses archived records and does not start a target.

## Verify session and stream results

```sh
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-sessions.txt
.venv/bin/python scripts/verify_sessions.py --output results/sessions
.venv/bin/python -m unittest discover -s tests/sessions -v
```

Verification checks all 840 trials, 108,000 emitted PacketIn IDs and 510 actual Write results. It reproduces per-trial outputs and all 62 statistical rows, with independent event decoding and NumPy checks of 496 summary values. It uses archived records without a target or Go toolchain. See the [session and stream method](docs/sessions.md).

## Verify batch-write retry results

```sh
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-retries.txt
.venv/bin/python scripts/verify_retries.py --output results/retries
.venv/bin/python -m unittest discover -s tests/retries -v
```

The verifier checks all 192 trials, 334 Writes, 882 update attempts and 112 application Reads. Protobuf and independent standard-library decoders reconstruct retry subsets and target state, then reproduce the historical reports and saved CSV files byte for byte. The retry policy belongs to the experiment application. See the [batch-retry method](docs/retries.md).

## Verify data-plane cases

```sh
python3 scripts/verify_dataplane.py --output results/dataplane
python3 -m unittest discover -s tests/dataplane -v
```

The verifier uses only the Python standard library. It checks all 130 cases, 520 captures, 360 counter checkpoints, 2,040 periodic samples and sixty target restarts. It reproduces both original CSV files and independently cross-checks frames, ports, table state, raw counter replies and CLI calls. See the [data-plane method](docs/dataplane.md).

The repository supports statistical reanalysis and archived input, request, session, retry and data-plane verification. Live runners cover RPC and construction measurements, input validation, sessions, streams and batch retries. Live data-plane case-study runners will follow in a subsequent batch.

## License

Source code and documentation use Apache-2.0. Experimental data and derived figures use CC BY 4.0. See [LICENSE.md](LICENSE.md).
