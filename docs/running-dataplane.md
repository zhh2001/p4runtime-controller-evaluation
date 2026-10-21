# Run the data-plane comparison

The E5 runners execute the [reactive forwarding, counter/recovery and CLI cases](dataplane.md) with two Mininet hosts and BMv2. They retain the original application logic, six software profiles, frame payloads, table checks and counter checkpoints.

## Setup

Use Linux x86_64, CPython 3.12 with `venv` support, Go, a C compiler, Mininet, `ip`, `p4c-bm2-ss` and `simple_switch_grpc`. The commands must be on `PATH`. Mininet's Python package must be available through `/usr/lib/python3/dist-packages`. The published environment and local verification use WSL2. Setup selects Go 1.26.9.

Run setup as your ordinary user:

```sh
python3 scripts/bootstrap_dataplane.py
```

Setup downloads and verifies the four pinned client sources, installs Finsy and shell in separate Python environments, and compares their revisions with the archived E5 environment. It compiles the bundled L2 P4 program and checks the compiler and compatible P4Info declarations against the archived fixtures. It runs `go vet` for both Go modules, builds ordinary and race-enabled Go drivers plus p4ctl, and runs the E5 archive and runner tests.

Logs are written to `results/dataplane-setup/`. `.runtime/dataplane-setup.json` records input and generated file hashes, dependency inventories, Python environment signatures, and compiler, target and Mininet versions.

The first setup needs network access or populated source, wheel and Go module caches. To supply Python wheels locally, use `finsy/` and `shell/` subdirectories populated from their respective files in `config/requirements-*.txt`:

```sh
python3 scripts/bootstrap_dataplane.py --wheelhouse /path/to/wheels
```

Source archives use their configured URLs or `.runtime/downloads/`. Go uses its module cache or proxy.

## Execute

Run the launcher through sudo so it can create network namespaces and raw Ethernet sockets:

```sh
sudo python3 scripts/run_dataplane.py --output results/dataplane-smoke
```

| Mode | Profiles | Repetitions per case/profile | CLI cases | Total cases |
| --- | --- | ---: | ---: | ---: |
| `smoke` | All six | 1 | 1 | 13 |
| `race` | SDK and legacy Go | 1 | 0 | 4 |
| `full` | All six | 10 | 10 | 130 |

Each profile runs reactive forwarding and counter/recovery. The profile order is shuffled within each repetition using the archived seed, 20261017. CLI cases follow the controller cases. Race mode uses the Go race detector for both selected drivers.

```sh
sudo python3 scripts/run_dataplane.py --mode race --output results/dataplane-race
sudo python3 scripts/run_dataplane.py --mode full --output results/dataplane-full
```

For a selected controller, use `--profiles`. Add `--skip-cli` to omit CLI cases:

```sh
sudo python3 scripts/run_dataplane.py --profiles sdk --skip-cli --output results/dataplane-sdk
```

The topology uses two host namespaces, data ports 1 and 2, CPU port 255 and device ID 1. Each case starts a fresh target with dynamically selected loopback ports. Interface names are unique to the run. The launcher runs controller drivers and p4ctl as the invoking user through `SUDO_UID` and `SUDO_GID`, then returns output ownership to that user.

The reactive case forwards two initial frames through PacketIn/PacketOut and 64 later frames through installed rules. The counter/recovery case verifies a sixteen-entry MODIFY batch, periodic counter reads, target restart, pipeline and rule restoration, and 384 forwarded frames. Its probe while the target is stopped must be dropped. Each CLI case checks seven invocations and an explicit PacketOut port that differs from the installed destination rule.

Output directories must be new and outside the published `data/` and `figures/` directories. Inputs, generated files, pinned sources and interpreter signatures are checked before and after execution. Rerun setup after changing these inputs. Each run closes its controller and host processes, stops its BMv2 targets, removes their IPC files and stops its own Mininet topology. It records checks that the switch interfaces and host namespace processes are gone. Ctrl+C or SIGTERM to the launcher also requests this cleanup and records the run as interrupted.

## Outputs and verification

| Path under the output directory | Contents |
| --- | --- |
| `execution.json` | Selected mode and profiles, setup record, completion or failure |
| `run.log`, `analysis.log` | Case progress and analysis output |
| `run/manifest.json` | Plan, randomized schedule, completed cases and source hashes |
| `run/source/` | Experiment and verification sources, fixtures, setup inputs and binaries |
| `run/environment.json`, `topology.json` | Setup versions and host/interface configuration |
| `run/<case>/` | Four PCAP files, host and driver records, table/counter observations, events and target logs |
| `run/network-cleanup.json` | Switch interface removal and namespace process exit checks |
| `run/artifact-hashes.json` | Execution artifact hashes |
| `run/case-results.csv`, `cli-invocations.csv` | Reconstructed case results and CLI timings |
| `run/independent-verification.json`, `verification.json` | Independent reconstruction and final verification |

The primary checker retains the archived frame and per-case verification functions. A separate standard-library decoder reconstructs PCAP frames, driver commands, table entries, counter results and CLI behavior. Both must agree on every case and all totals. Analysis also checks the schedule, snapshot and artifact hashes, interface cleanup and target IPC removal.

A full run expects 27,170 received frames, sixty stopped-target drops, 360 counter checkpoints, sixty target restarts and seventy successful CLI calls. Periodic counter sample counts and CLI elapsed times come from the new run. They are not required to equal the archived measurements. These are functional cases through a common application and adapter protocol.

Repeat analysis of a completed local run with:

```sh
.runtime/finsy/bin/python scripts/analyze_dataplane.py results/dataplane-full/run
```

To verify the published records without starting Mininet or BMv2, use the [offline archive instructions](dataplane-data.md).
