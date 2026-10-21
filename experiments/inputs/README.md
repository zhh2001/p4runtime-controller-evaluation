# Input-validation runners

These runners execute the baseline, numerical-boundary and table-constraint corpora on a controlled P4Runtime service and BMv2.

```sh
python3 scripts/bootstrap_inputs.py
python3 scripts/run_inputs.py --output results/inputs-smoke
python3 scripts/run_inputs.py --mode full --output results/inputs-full
python3 scripts/run_inputs.py --mode race --output results/inputs-race
```

Run these commands from the repository root. See [Run the input-validation comparison](../../docs/running-inputs.md) for prerequisites, profile coverage, output files and analysis. The [input method](../../docs/inputs.md) describes the archived inputs and results.

`baseline/`, `boundaries/` and `constraints/` contain the drivers, controlled services, models and tests. `common/` supplies BMv2 process and RPC recording helpers. Setup creates ignored `fixtures/` and `bin/` directories in each group, then checks generated inputs against the archive.
