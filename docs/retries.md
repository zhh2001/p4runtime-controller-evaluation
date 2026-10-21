# Batch-write retries

The E4 archive contains 192 application trials recorded on 2026-10-19. It tests how complete per-update outcomes and table readback support selective retries under `CONTINUE_ON_ERROR`. Each of six profiles runs ten controlled-service cases and six BMv2 cases twice.

## Verify the archive

Use CPython 3.12 on Linux x86_64:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-retries.txt
.venv/bin/python -m pip check
.venv/bin/python scripts/verify_retries.py --output results/retries
.venv/bin/python -m unittest discover -s tests/retries -v
```

The output directory must be new. Verification checks the archive inventory, compressed and uncompressed hashes, exact trial order, RPC association, driver output and fixture correspondence. It decodes binary requests and responses using the recorded protobuf definitions, then runs a second decoder using only the Python standard library. Both reconstruct update outcomes, original operation indexes, retry subsets and final table state. Both regenerated reports must match the historical reports byte for byte. The verifier also reproduces the three summary CSV files. Analysis needs no running target or Go toolchain.

## Profiles and execution

| Profile | Write interface and outcome handling |
| --- | --- |
| sdk | SDK `Write`, using the complete public `WriteError.Updates` slice |
| raw-current | Generated Go RPCs with the SDK dependency stack, with application status decoding |
| legacy | Pinned legacy Go client, with application status decoding |
| raw-legacy | Generated Go RPCs with the legacy dependency stack, with application status decoding |
| finsy | Finsy `P4Client.request`, using its public failed-index map and an application status-envelope check |
| shell | Shell public client `write`, using its public error results and an application status-envelope check when the call returns |

These are the six pinned software profiles in the [RPC method](rpc-method.md#software-and-host). The SDK revision is `3f2def2b28c098961078ce64e9d86ab3c79e6ccb`, Finsy is v0.30.0, the legacy client is `de3748d9d85953f1fa7093ca441aca53dfc9008b`, and shell is `5641d39c1778c79f726da7e216b2c7e134af4a47`. The original run manifests retain source hashes and commit IDs. The host is WSL2, Linux 6.6.87.2, with an 8 GiB memory limit and 2 GiB swap. BMv2 uses build 1.15.0-2bdd0b7b.

Trials run sequentially in repetition, profile and case order. Each starts a separate application process and a fresh table state. BMv2 reinstalls the pipeline before each trial, then installs that case's seed entries. An independent observer reads the state before execution, after each Write and after application exit. Controlled-service snapshots copy the service state. There are no concurrent writers, and each original operation addresses a distinct key.

The application allows at most four Write attempts and waits at most six seconds for each driver operation. Go calls, shell Writes and the relay's target RPCs have five-second deadlines. The Finsy Write uses its native request path. Shell's wrapper adds the RPC deadline and captures the raw error without changing the library parser. The six-second watchdog can therefore terminate a process that remains in local error handling after the RPC has returned.

## Retry policy

The common application maintains an `original_index` for each intended operation. A retry's local result position is mapped through its pending subset to that original index.

- A complete `OK` result removes that operation from the pending set.
- Per-update `UNAVAILABLE` and `RESOURCE_EXHAUSTED` retry the original operation. The controlled service defines these injected failures as unapplied.
- Permanent per-update errors stop that operation and retain successful changes.
- `ALREADY_EXISTS` and `NOT_FOUND` require a Read. An existing INSERT becomes MODIFY, a missing MODIFY becomes INSERT, and a missing DELETE is already satisfied.
- An RPC-wide error or incomplete status requires a Read before selecting any retry. Operations whose desired state is already present are omitted.

All profiles use this application policy. The SDK does not retry writes automatically. The Finsy and shell envelope checks belong to the application and are separate from their public error maps. They check the status code, detail count, Any message type and per-update code range. The SDK path uses its public complete results, including successful items.

For example, the five-operation `transient-rebased` case returns `[OK, UNAVAILABLE, OK, RESOURCE_EXHAUSTED, OK]`. The first retry contains original indexes `[1, 3]`. Its results are `[OK, UNAVAILABLE]`, so the next retry contains original index `[3]`. No successful original operation is sent again.

## Scenarios

| Target | Cases | Trials |
| --- | --- | ---: |
| Controlled | All OK, one transient failure, rebased retry indexes, all transient failures, permanent error, short details, wrong Any type, missing details, partially applied RPC-wide failure, fully applied RPC-wide failure | 120 |
| BMv2 | All OK, duplicate in the middle, mixed conflicts, all duplicates, missing DELETE, single duplicate | 72 |

The controlled service applies its scheduled successful updates before returning the chosen status. In the two RPC-wide failure cases it returns `UNAVAILABLE` after partial or complete application. Short details contain one failure detail for a three-update request. The wrong-type case replaces one `p4.Error` detail with `TableEntry`. Missing details have no status-details trailer.

BMv2 supplies its own per-update statuses through a recording relay. The mixed-conflict case sends INSERT-new, INSERT-existing, MODIFY-missing, DELETE-missing and MODIFY-existing. Its first outcomes are `[OK, ALREADY_EXISTS, NOT_FOUND, NOT_FOUND, OK]`. Readback leads to retries of original indexes `[1, 2]` as MODIFY and INSERT. The missing DELETE needs no retry.

## Results

Each profile has 32 trials across both targets and repetitions.

| Profile | Complete desired state | Stopped on permanent error | Watchdog termination |
| --- | ---: | ---: | ---: |
| sdk | 30 | 2 | 0 |
| raw-current | 30 | 2 | 0 |
| legacy | 30 | 2 | 0 |
| raw-legacy | 30 | 2 | 0 |
| finsy | 30 | 2 | 0 |
| shell | 16 | 0 | 16 |

The ten permanent-error trials retain successful operations and stop on original index 1. The sixteen shell terminations comprise ten controlled-service and six BMv2 trials. In these cases, the pinned shell parser does not return when it encounters an OK item in a mixed result list. All-failure responses return normally. The `all-transient` case returns from its first all-failure response, then stalls on the mixed response to its retry. In the wrong-type case, the parser stalls at the leading OK item before reaching the wrong Any type. Independent snapshots preserve the target's actual state after termination.

| Target | Trials | Writes | Update attempts | Successful update attempts | Failed update attempts | Application Reads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Controlled | 120 | 218 | 540 | 386 | 154 | 58 |
| BMv2 | 72 | 116 | 342 | 234 | 108 | 54 |
| Total | 192 | 334 | 882 | 620 | 262 | 112 |

These counts include retries and exclude setup calls and independent observer Reads. In every `transport-complete` trial, a single Write is followed by a Read confirming the desired state, with no resend. Across all trials, no successful original operation is replayed. Both decoders verify the state reached by complete, blocked and watchdog-terminated applications.

The results describe these fixed functional scenarios. They measure neither throughput nor a reliability rate. RPC-wide ambiguity here is injected by the controlled service. Actual stream faults and target restarts are covered separately by the [session comparison](sessions.md).

## Archive and source correspondence

[manifest.json](../data/retries/manifest.json) identifies the two complete formal runs and every exported file. Smoke and race runs are outside this selection. Original observation, RPC, run-manifest and analysis-report bytes are retained. Driver stdout and stderr are collected as text with each original file hash. Source inventories retain hashes of the original drivers and executables, while this batch exports the offline checks and fixtures.

[retry-source-provenance.json](../config/retry-source-provenance.json) binds the analysis code to both execution snapshots. The independent wire decoder, retry model and status guard are unchanged. The primary analyzer uses a package import and checks recorded process-exit fields in place of historical IPC paths. All thirteen original policy and status tests retain their function bodies. Additional tests exercise archive corruption, missing trials, request changes, original-index mapping and final-state reconstruction. See the [data dictionary](retry-data.md) for record fields and regenerated outputs.
