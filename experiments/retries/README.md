# Batch-write retry experiments

These drivers run the fixed E4 application retry policy through the pinned SDK,
legacy Go client, Finsy and shell interfaces, plus the two matched raw Go stacks.
The controlled service supplies scheduled failures and ambiguous RPC outcomes.
The BMv2 relay records the target's own statuses and independent table snapshots.

From the repository root:

```sh
python3 scripts/bootstrap_retries.py
python3 scripts/run_retries.py --output results/retries-smoke
```

Use `--mode race` for all 32 SDK and legacy Go trials with the race detector, or
`--mode full` for the complete 192-trial plan. `--targets` selects `controlled`,
`bmv2`, or both. See [Run the batch-write retry comparison](../../docs/running-retries.md)
for setup, outputs and verification.

The application decides which operations to retry. It preserves original indexes,
reads before reconciling conflicts or uncertain outcomes, and omits successful
operations. The SDK does not retry writes automatically. The source correspondence
is recorded in [retry-runner-provenance.json](../../config/retry-runner-provenance.json).
