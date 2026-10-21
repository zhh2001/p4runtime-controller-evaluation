# Input validation and encoding

The E1 archive contains 382 fixed inputs and 6,984 trials across six profiles and two repetitions. It combines the baseline match corpus, numerical and typed-data boundaries, and table-entry constraints. Controlled-service and BMv2 results are stored separately.

## Verify the archive

Use CPython 3.12 on Linux x86_64. Install the pinned analysis packages, including the protobuf definitions used to decode the records:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r config/requirements-inputs.txt
.venv/bin/python -m pip check
.venv/bin/python scripts/verify_inputs.py --output results/inputs
```

The output directory must be new. Verification checks the complete file inventory, compressed and uncompressed SHA-256 hashes, case coverage, request association, repeated classifications and request/readback semantics. It reproduces all four pairs of extended-run `case-results.csv` and `counts.csv` files, and both table-constraint finding files, byte for byte. Independent JSON, base64 and integer decoders also check the numerical and table-state results. This analysis runs from saved records without a switch or Go toolchain.

Run the input-analysis tests with:

```sh
.venv/bin/python -m unittest discover -s tests/inputs -v
```

## Corpus and runs

| Group | Fixed inputs | Controlled trials | BMv2 trials |
| --- | ---: | ---: | ---: |
| Baseline matches and actions | 28 | 336 | 336 |
| Numerical, index and typed-data boundaries | 286 | 3,432 | 1,248 |
| Table-entry constraints | 68 | 816 | 816 |

Every selected case is run once per profile in each of two repetitions. BMv2 runs 104 of the 286 numerical cases: wide table matches, action parameters and ordinary counter/meter indices. Typed registers and translated-index mappings are exercised by the controlled service.

The profiles are SDK v2.0.1, the pinned legacy Go client, Finsy v0.30.0 and p4runtime-shell, plus two raw Go protocol profiles matching the respective Go dependency stacks. [The catalog](../data/inputs/manifest.json) records their commits, Go version and Python protobuf backends. The baseline trials were recorded on 2026-10-17 and the two extensions on 2026-10-19 in WSL2.

### Baseline matches and actions

The 28 inputs comprise nine canonical inputs, thirteen invalid inputs and six normalizable inputs. They exercise EXACT, LPM, TERNARY, RANGE and OPTIONAL matches, names, required fields, action parameters, priorities, leading zeros and wildcard forms. All six profiles accept all nine canonical inputs on both targets. Invalid-input classifications are the same on both targets. Counts below are per repetition.

| Profile | Invalid: local rejection | Invalid: target rejection | Invalid: unavailable | Normalizable accepted, controlled | Normalizable accepted, BMv2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| sdk | 13 | 0 | 0 | 6 | 6 |
| raw-current | 0 | 13 | 0 | 1 | 2 |
| legacy | 0 | 12 | 1 | 3 | 4 |
| raw-legacy | 0 | 13 | 0 | 1 | 2 |
| finsy | 8 | 5 | 0 | 4 | 5 |
| shell | 6 | 7 | 0 | 3 | 3 |

BMv2 accepts an explicit full-range RANGE and returns this representation for an omitted RANGE wildcard. The verifier normalizes that readback for semantic comparison. Ten accepted readbacks in the two recorded repetitions use this normalization. The original `state_semantics_match` flags remain in the observations, together with the later readback evaluations. OPTIONAL zero and EXACT zero remain explicit matches.

### Numerical, index and typed-data boundaries

The selected widths are 1, 7, 8, 9, 16, 32, 48, 64, 128 and 129 bits. Cases cover unsigned and signed limits, leading zeros and sign extension, ordinary and translated indices, tuple, struct, named types, translated integers and strings, varbit and nested data. The controlled corpus has 170 valid or normalizable inputs and 116 invalid inputs. The BMv2 subset has 70 and 34 respectively.

Varbit checks compare the explicit dynamic width and numerical content.

| Target | Profile | Valid accepted | Valid unavailable | Invalid local | Invalid target | Invalid unavailable |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| controlled | sdk | 170 | 0 | 92 | 24 | 0 |
| controlled | raw-current | 170 | 0 | 0 | 116 | 0 |
| controlled | legacy | 76 | 94 | 0 | 46 | 70 |
| controlled | raw-legacy | 170 | 0 | 0 | 116 | 0 |
| controlled | finsy | 160 | 10 | 62 | 54 | 0 |
| controlled | shell | 80 | 90 | 12 | 44 | 60 |
| bmv2 | sdk | 70 | 0 | 34 | 0 | 0 |
| bmv2 | raw-current | 70 | 0 | 0 | 34 | 0 |
| bmv2 | legacy | 68 | 2 | 0 | 30 | 4 |
| bmv2 | raw-legacy | 70 | 0 | 0 | 34 | 0 |
| bmv2 | finsy | 70 | 0 | 20 | 14 | 0 |
| bmv2 | shell | 70 | 0 | 12 | 22 | 0 |

All accepted requests preserve the intended numerical and typed values. SDK and Finsy locally reject the same 62 numerical or compound-data errors in each controlled repetition. SDK additionally rejects 30 index errors locally. The remaining 24 SDK target rejections concern translated indices outside the service mapping. The controlled mapping is 10000 to array index 0 and 10001 to index 7. Its contents are not in P4Info.

Unavailable entries describe the selected high-level API. The legacy client has no dedicated register or meter writer, shell has no register entity, and Finsy signed interfaces accept integers rather than the ten sign-extended byte-array inputs. The raw Go profiles retain protocol-level access.

### Table-entry constraints

The 68 inputs comprise 27 valid or normalizable inputs, 39 invalid inputs and two API-policy inputs. They cover default actions and reset, constant tables, action scope, direct and indirect references, sparse IDs, update types, TTL and priorities. The two policy inputs retain a stale match or priority when switching to a default entry. Their results remain separate in the CSV.

The valid columns below report accepted / rejected / unavailable. Invalid columns report local rejection / target rejection / accepted / unavailable. Each cell is one repetition.

| Target | Profile | Valid: accepted / rejected / unavailable | Invalid: local / target / accepted / unavailable |
| --- | --- | --- | --- |
| controlled | sdk | 24 / 0 / 3 | 24 / 4 / 0 / 11 |
| controlled | raw-current | 27 / 0 / 0 | 0 / 39 / 0 / 0 |
| controlled | legacy | 22 / 5 / 0 | 0 / 39 / 0 / 0 |
| controlled | raw-legacy | 27 / 0 / 0 | 0 / 39 / 0 / 0 |
| controlled | finsy | 26 / 1 / 0 | 4 / 35 / 0 / 0 |
| controlled | shell | 26 / 1 / 0 | 9 / 30 / 0 / 0 |
| bmv2 | sdk | 24 / 0 / 3 | 24 / 4 / 0 / 11 |
| bmv2 | raw-current | 27 / 0 / 0 | 0 / 37 / 2 / 0 |
| bmv2 | legacy | 22 / 5 / 0 | 0 / 37 / 2 / 0 |
| bmv2 | raw-legacy | 27 / 0 / 0 | 0 / 37 / 2 / 0 |
| bmv2 | finsy | 26 / 1 / 0 | 4 / 33 / 2 / 0 |
| bmv2 | shell | 26 / 1 / 0 | 9 / 28 / 2 / 0 |

The SDK Builder expresses direct actions. Three valid and eleven invalid indirect-reference inputs are therefore unavailable through this path. The legacy action helper assigns parameter IDs by position, so five valid sparse-ID inputs are rejected by both targets. Finsy and shell reject a DELETE carrying an unrelated action before sending it, while the other profiles complete the key-only deletion.

BMv2 accepts an EXACT MODIFY with `priority=-1` and a default MODIFY with `idle_timeout_ns=1` in five profiles. The corresponding readbacks set priority or TTL to zero. These twenty observations are retained as findings, along with 62 rejections whose per-item status differs from the controlled oracle. All rejected operations retain their prior state.

The rules use the [P4Runtime 1.3.0 baseline](https://p4lang.github.io/p4runtime/spec/v1.3.0/P4Runtime-Spec.html#sec-table-entry). In that version, TCAM-like tables require nonzero priority, while other tables require zero. The negative TCAM-priority MODIFY case addresses a key absent from initial state and expects NOT_FOUND. The SDK's positive-priority requirement is an API policy. Default-entry TTL must be zero.

## Files and provenance

| Material | Location |
| --- | --- |
| Cases, P4Info and P4 programs | [baseline](../data/inputs/baseline/fixtures), [boundaries](../data/inputs/boundaries/fixtures), [constraints](../data/inputs/constraints/fixtures) |
| Selected runs, file sizes and hashes | [manifest.json](../data/inputs/manifest.json) |
| Per-trial records, requests and complete saved target snapshots | `data/inputs/<group>/<target>/*.jsonl.gz` |
| Original extended-run classifications and counts | `data/inputs/<group>/<target>/*.csv` |
| Field definitions and outcome names | [Input data dictionary](input-data.md) |
| Verification entry point | [verify_inputs.py](../scripts/verify_inputs.py) |
| Frozen analysis logic and independent decoders | [input_checks](../scripts/input_checks) |
| Original and exported analysis-source hashes | [input-source-provenance.json](../config/input-source-provenance.json) |

The extended observations and RPC files retain their original bytes inside gzip streams with timestamp zero. The baseline export selects E1 observation and controlled-request lines from the combined E1/E4 records. Its BMv2 RPC file selects the completed Write events referenced by those observations. Header and row bytes are preserved when selecting baseline counts. The corpus selects the same 28 E1 cases. E4 trials and atomicity probes are outside this archive.

Run metadata selects case IDs, source inventories, timestamps and completion fields from the original manifests. It omits local process paths. The catalog records each source manifest hash, original file hash and export transformation. The four extended-run result/count pairs and two findings files preserve their original bytes.

The primary analysis functions and numerical models retain their original bodies. Imports and unused workspace constants are adapted for the package layout. Independent verification uses the original JSON/base64/integer decoding functions with per-run entry points. Twenty-three original oracle tests retain their test methods and use the archived fixtures. New checks cover inventory corruption, incomplete coverage, changed payloads and changed readbacks.

The verifier writes regenerated per-run CSV files, a combined `case-results.csv`, per-repetition `counts.csv` and `verification.json` under the requested output directory. Archived data remains unchanged. This batch provides offline verification of existing trials. Live experiment runners will be added separately.

Experimental data and fixtures use CC BY 4.0. Analysis code and documentation use Apache-2.0. See [LICENSE.md](../LICENSE.md).
