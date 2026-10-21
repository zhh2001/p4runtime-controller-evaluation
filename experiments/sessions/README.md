# Session and stream runners

Run these commands from the repository root:

```sh
python3 scripts/bootstrap_sessions.py
python3 scripts/run_sessions.py --output results/sessions-smoke
python3 scripts/run_sessions.py --mode race --output results/sessions-race
python3 scripts/run_sessions.py --mode full --output results/sessions-full
```

See [Run the session and stream comparison](../../docs/running-sessions.md) for prerequisites, group selection, output files and analysis. The [method](../../docs/sessions.md) describes the archived conditions and results.

`recover.py`, `mastership.py`, `cancellation.py` and `consumers.py` contain the four trial functions. `infra.py`, `server.py` and `common.py` provide the target, relay, controlled service and driver helpers. `sdk/` and `session-sdk/` are the Go modules. Setup creates ignored `fixtures/` and `bin/` directories and compares generated declarations with the archive.
