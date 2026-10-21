"""Execute the selected baseline E1 cases against a service or BMv2."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import grpc
from finsy.proto import p4i, p4r_grpc
from google.protobuf import text_format

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(ROOT.parent / "common"))
from bootstrap_inputs import check_setup, check_versions, snapshot_run
from comparisons import semantic_match, compare_failures
from invoke import run_child
from server import Endpoint
from infra import Relay, Session, Target, Trace, message


SMOKE = {"exact-zero", "exact-overflow", "lpm-aligned", "ternary-canonical", "range-canonical",
         "range-full", "optional-zero", "action-unknown-param"}
RACE = {"exact-zero", "exact-overflow", "range-canonical", "optional-zero"}


def commands(race):
    suffix = "-race" if race else ""
    result = {"sdk": [str(ROOT / "bin" / ("go-sdk" + suffix)), "--adapter", "native"],
              "legacy": [str(ROOT / "bin" / ("go-legacy" + suffix)), "--adapter", "native"]}
    if not race:
        result.update({"raw-current": [str(ROOT / "bin/go-sdk"), "--adapter", "raw"],
                       "raw-legacy": [str(ROOT / "bin/go-legacy"), "--adapter", "raw"],
                       "finsy": [str(REPOSITORY / ".runtime/finsy/bin/python"), str(ROOT / "adapters/finsy_adapter.py")],
                       "shell": [str(REPOSITORY / ".runtime/shell/bin/python"), str(ROOT / "adapters/shell.py")]})
    return result


def controlled(output, cases, profiles):
    info = text_format.Parse((ROOT / "fixtures/table_keys.p4info.txt").read_text(), p4i.P4Info())
    rows = []
    with (output / "rpc.jsonl").open("w") as log:
        endpoint = Endpoint(info, cases, log)
        server = grpc.server(ThreadPoolExecutor(max_workers=8))
        p4r_grpc.add_P4RuntimeServicer_to_server(endpoint, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            for repetition in (1, 2):
                for case in cases:
                    for profile, command in profiles.items():
                        argv = command + ([f"127.0.0.1:{port}", str(ROOT / "fixtures/table_keys.p4info.txt")]
                                          if profile in ("finsy", "shell") else
                                          ["--address", f"127.0.0.1:{port}", "--p4info", str(ROOT / "fixtures/table_keys.p4info.txt")])
                        token = f"{profile}@{repetition}"
                        env = dict(os.environ, P4_ADAPTER=token)
                        started = time.monotonic_ns()
                        (output / "logs" / f"{repetition}-{case['id']}-{profile}-command.json").write_text(json.dumps(argv, indent=2) + "\n")
                        process = subprocess.run(argv, input=json.dumps(case), capture_output=True, text=True, env=env, timeout=5)
                        row = json.loads(process.stdout) if process.returncode == 0 else dict(stage="process_error", exit_code=process.returncode, stdout=process.stdout)
                        row.update(adapter=profile, token=token, repetition=repetition, suite="E1", case_id=case["id"],
                                   category=case["category"], elapsed_ns=time.monotonic_ns() - started)
                        if process.stderr:
                            (output / "logs" / f"{repetition}-{case['id']}-{profile}.stderr").write_text(process.stderr)
                        rows.append(row)
                        with (output / "observations.jsonl").open("a") as stream:
                            stream.write(json.dumps(row) + "\n")
                print(json.dumps(dict(mode="controlled", repetition=repetition, observations=len(rows))), flush=True)
        finally:
            server.stop(0).wait(10)
    events = [json.loads(line) for line in (output / "rpc.jsonl").read_text().splitlines()]
    indexed = {(event["adapter"], event["case_id"]): event for event in events}
    corpus = {case["id"]: case for case in cases}
    for row in rows:
        event = indexed.get((row["token"], row["case_id"]))
        row["rpc_observed"] = event is not None
        outcome = row["stage"]
        if event and row["stage"] == "request_completed":
            outcome = "server_rejected" if any(event["codes"]) else "accepted"
            if outcome == "accepted":
                case = corpus[row["case_id"]]
                row["semantics_match"] = case["category"] != "invalid" and semantic_match(case, event["request"], info)
        row["outcome"] = outcome
    return rows


def bmv2(output, cases, profiles):
    target = Target(output / "target")
    trace = Trace(output / "rpc.jsonl")
    relay = None
    rows = []
    try:
        target.start()
        relay = Relay(target.address, trace)
        for repetition in (1, 2):
            for case in cases:
                for profile, command in profiles.items():
                    controller = Session(relay.address, arbitrate=True)
                    try:
                        controller.install()
                        before = controller.read()
                        if profile in ("sdk", "shell"):
                            controller.close()
                            controller = None
                        offset = len(trace.rows)
                        row = run_child(profile, command, case, relay.address, repetition, output / "logs")
                        observer = Session(relay.address)
                        try:
                            after = observer.read()
                        finally:
                            observer.close()
                        token = f"E1|{case['id']}|{profile}@{repetition}"
                        events = [event for event in trace.rows[offset:] if event["event"] == "write_end" and
                                  base64.b64decode(event["request"]["updates"][0]["entity"]["table_entry"].get("metadata", "")).decode() == token]
                        row.update(before=[message(e) for e in before], after=[message(e) for e in after],
                                   write_events=events, rpc_observed=bool(events))
                        rows.append(row)
                        with (output / "observations.jsonl").open("a") as stream:
                            stream.write(json.dumps(row) + "\n")
                    finally:
                        if controller:
                            controller.close()
            print(json.dumps(dict(mode="bmv2", repetition=repetition, observations=len(rows))), flush=True)
    finally:
        if relay:
            relay.close()
        target.stop()
        trace.close()
        (output / "target-processes.json").write_text(json.dumps(target.history, indent=2) + "\n")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("controlled", "bmv2"), default="controlled")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--race", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so semantic checks remain enabled.")
    cases = json.loads((ROOT / "fixtures/cases.json").read_text())
    if args.smoke or args.race:
        names = RACE if args.race else SMOKE
        cases = [case for case in cases if case["id"] in names]
    profiles = commands(args.race)
    versions = check_versions()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    manifest = dict(started_local=datetime.now().astimezone().isoformat(), mode=args.mode, smoke=args.smoke, race=args.race,
                    case_ids=[case["id"] for case in cases], profiles=list(profiles), repetitions=2, observations=0,
                    source_sha256=snapshot_run(output, "baseline"), versions=versions, completed=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        rows = (controlled if args.mode == "controlled" else bmv2)(output, cases, profiles)
        (output / "observations.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        manifest["observations"] = len(rows)
        check_setup()
        manifest.update(completed=True, completed_local=datetime.now().astimezone().isoformat())
    finally:
        if (output / "observations.jsonl").exists():
            manifest["observations"] = len((output / "observations.jsonl").read_text().splitlines())
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(output=str(output), observations=len(rows), completed=True)), flush=True)


if __name__ == "__main__":
    main()
