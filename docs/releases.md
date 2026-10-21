# Evaluation releases

The [evaluation-v1 Release](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/evaluation-v1), published on October 22, 2026, contains the complete evaluation snapshot at commit `edbb75d7b83d27dce6a3138aa01122921773f7ab`. It covers all six datasets and their analysis and execution tools. The evaluated SDK is p4runtime-go-controller v2.0.1. The [release notes](releases/evaluation-v1.md) describe the contents and citation.

Download `evaluation-v1.tar.gz`, `evaluation-v1-manifest.json` and `SHA256SUMS` from the Release, place them in one directory and run `sha256sum -c SHA256SUMS` before extracting the archive.

## Build and check the release files

From a Git checkout with the fixed commit available, use Python 3.12:

```sh
python3 scripts/package_release.py --output results/releases/evaluation-v1
python3 scripts/package_release.py --verify results/releases/evaluation-v1
```

The [release specification](../config/evaluation-v1.json) identifies the source commit, archive root, SDK and RPC trace catalog. Packaging reads committed Git objects. It checks every archived file against its Git object and preserves its mode. The tar uses the commit's timestamps and the gzip header has a zero timestamp and no local filename. The manifest and checksum file contain no build time or local paths.

| Release file | Contents |
| --- | --- |
| `evaluation-v1.tar.gz` | The 728-file source and data snapshot, rooted at `p4runtime-controller-evaluation-v1/` |
| `evaluation-v1-manifest.json` | Source commit, file sizes, SHA-256 hashes, Git blob IDs, modes and external RPC archive records |
| `SHA256SUMS` | SHA-256 hashes of the source archive and manifest |

The original RPC request archives remain in the [rpc-traces-v1 Release](https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/rpc-traces-v1). Their sizes and hashes are bound by the catalog in the source snapshot and are copied into the release manifest.

To check downloaded release files, use `--verify` with their directory. This check uses the Python standard library and does not read Git metadata. It checks both container hashes, then every archived file, mode and Git blob digest. It also rejects duplicate files and paths outside the archive root.

## Source and verification

The `evaluation-v1` tag points to the fixed source commit above. The packaging helper and release documentation are maintained on `main`; they do not change the frozen snapshot. The archive itself contains the existing reproduction guide, analysis scripts and live runners.

All three published files were downloaded and verified against the prepared assets and the 728-file manifest. The downloaded snapshot passed all 202 tests and six offline analyses. Use the versioned Release URL in the [citation](releases/evaluation-v1.md#citation-and-license).
