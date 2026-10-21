# Data-plane experiments

These drivers run the E5 reactive forwarding and counter/recovery cases through the pinned SDK, legacy Go client, Finsy and shell interfaces, plus the two matched raw Go stacks. A separate case checks p4ctl. Two Mininet hosts send and capture the Ethernet frames.

From the repository root:

```sh
python3 scripts/bootstrap_dataplane.py
sudo python3 scripts/run_dataplane.py --output results/dataplane-smoke
```

The default runs thirteen cases. Use `--mode race` for four SDK and legacy Go cases with the race detector, or `--mode full` for all 130 cases. `--profiles` selects controllers, and `--skip-cli` omits p4ctl. See [Run the data-plane comparison](../../docs/running-dataplane.md) for prerequisites, outputs and checks.

The application handles destination rules, packet forwarding, counter polling and target restart recovery. The original case methods are retained. Path changes, setup and process cleanup are recorded in [dataplane-runner-provenance.json](../../config/dataplane-runner-provenance.json). Live records are checked with the archived verifier and an independent decoder.
