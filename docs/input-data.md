# Input-validation data dictionary

Each case is identified by group and `id`. Each trial is identified by group, target mode, profile, repetition and case ID. Two repetitions are recorded for every selected case/profile pair.

## Files

| File | Fields and purpose |
| --- | --- |
| `fixtures/cases.json` | Semantic inputs, category, API input representation and expected values |
| `fixtures/*.p4info.txt` | Declarations used to interpret resource IDs, widths, types and constraints |
| `fixtures/schema.json` | Table and action IDs used by the independent table-state decoder |
| `run.json` | Selected cases, profiles, repetitions, completion fields and source-manifest hash |
| `observations.jsonl.gz` | One JSON object per trial, including errors, public results and saved state |
| `rpc.jsonl.gz` | Requests and responses from the tested call path |
| `readback-evaluation.jsonl.gz` | Baseline BMv2 semantic comparisons after wildcard normalization |
| `case-results.csv` | Original per-trial classifications for the two extended corpora |
| `counts.csv` | Original counts by profile, family, category and outcome, across both repetitions |
| `recorded-counts.csv` | Baseline counts for one repetition in the original format |
| `findings.json` | Table-constraint observations requiring separate interpretation |

Gzip streams have timestamp zero. The catalog records both compressed and uncompressed sizes and SHA-256 hashes. Decompression preserves selected JSONL bytes and their row order. JSON bytestrings use the protobuf JSON base64 representation. Protobuf 64-bit integer fields are generally JSON strings.

## Cases and observations

Baseline categories are `canonical_valid`, `normalizable` and `invalid`. The extended corpora use `valid`, `normalizable` and `invalid`. Table constraints add `policy` for default-entry API behavior. Case categories describe the specified operation and input, including whether its addressed key or indirect reference exists.

Boundary cases specify `family`, `kind`, `op`, input bytes or typed `data`, and `expected` semantics. Register values include signed integers, typed composites, translated strings and explicit varbit width. Resource cases specify an index and whether its type is translated. Read index -1 means all entries and is encoded by omitting the protobuf Index.

Table-constraint cases specify the update type, table, matches, default flag, priority, TTL, action kind, parameters and indirect reference. `schema.json` preserves sparse field IDs 13 and 5 and parameter IDs 19 and 7. Before/after snapshots include table entries, default entries and profile members/groups.

The baseline uses `adapter` and `case_id`; extended observations use `profile`, `id` and an embedded `case`. Extended `token` values link a repetition/profile/case triple to `rpc_sequences`. Baseline request metadata carries the E1 case ID and adapter/repetition token. The verifier checks these associations.

`elapsed_ns` records process or adapter execution duration used in the original functional runs. It does not enter the count summaries. `monotonic_ns` orders recorded RPC events. Request and snapshot numeric values are decoded using their declared type and width.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `accepted` | The tested call reached the endpoint and completed successfully |
| `local_rejected` | The selected API reported an error before a tested-path RPC was observed |
| `target_rejected` | The endpoint rejected the observed request |
| `not_expressible` | The selected high-level path does not accept that input or resource combination |
| `local_panic` | The adapter recovered a panic before a request was observed |

Baseline raw observations use `server_rejected` and `unsupported_case` for the corresponding target-rejection and unavailable outcomes. The combined regenerated CSV uses the names above. The extended analyzer also distinguishes empty reads and public errors after empty reads if encountered. Neither occurs in the selected formal results.

`rpc_code` is the top-level numeric gRPC status. Write failure details preserve per-update P4Runtime canonical codes. These are distinct fields. `rpc_observed` records whether the tested call reached the controlled service or proxy. Setup, arbitration and independent snapshot reads are separate from the tested RPC count.

`semantics_match` compares an accepted request with the specified input. Table-constraint `state_matches_request` compares full post-call state with the change requested on the wire. This field can be false when BMv2 accepts and normalizes a field. Rejected operations must preserve state. The baseline's original `state_semantics_match` used structural comparison; the saved readback evaluations and public verifier account for explicit wildcard representations.

## Regenerated outputs

The top-level `case-results.csv` adds group and target mode and combines all 6,984 trials. Fields unavailable in a corpus remain empty. The top-level `counts.csv` groups by group, mode, profile, category and outcome. Its `count_per_round` is a single repetition's count, after checking that the two repetitions agree. Original extended-run `counts.csv` files instead sum both repetitions and retain family-level groups.

Table-constraint flags retain valid-input rejections, invalid-wire acceptances, per-item status differences and readback changes. See the [method and findings](inputs.md#table-entry-constraints). `verification.json` records per-run checks, source/catalog hashes and regenerated output hashes.
