import argparse
import json
import queue

from finsy.proto import p4r

from common import ROOT, Relay, Session, SessionDriver, Target, Trace, entry, message, snapshot, wait_until


def trial(adapter, repetition, relay, trace, output):
    seed = Session(relay.address, arbitrate=True)
    try:
        seed.install()
    finally:
        seed.close()
    driver = SessionDriver(adapter, relay.address, output / f"{adapter}-{repetition}.stderr")
    observer = Session(relay.address)
    higher = Session(relay.address)
    offset = len(trace.rows)
    try:
        initial = driver.command("write")
        assert initial["rpc_code"] == 0
        stream_id = next(row["stream_id"] for row in reversed(trace.rows) if row["event"] == "arbitration_response")
        requests = queue.Queue()
        def iterator():
            while (request := requests.get()) is not None:
                yield request
        higher.call = higher.stub.StreamChannel(iterator())
        higher.queue = requests
        request = p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(
            device_id=1, election_id=p4r.Uint128(low=2)))
        takeover = trace.add("takeover_requested", adapter=adapter, repetition=repetition)
        requests.put(request)
        response = next(higher.call)
        assert response.arbitration.status.code == 0
        demotion = trace.wait(lambda row: row["event"] == "arbitration_response"
                              and row["stream_id"] == stream_id
                              and row["sequence"] > takeover["sequence"]
                              and row["response"]["arbitration"]["status"].get("code", 0) == 6)
        def backup():
            state = driver.command("state")
            return state if not state["is_primary"] else None
        state = wait_until(backup)
        observed = trace.add("backup_observed", state=state)
        before = len([row for row in trace.rows if row["event"] == "write_start"])
        denied = driver.command("modify")
        after = len([row for row in trace.rows if row["event"] == "write_start"])
        assert not denied["is_primary"]
        if adapter == "sdk":
            assert denied["not_primary"] and before == after
        else:
            assert denied["rpc_code"] == 7 and after == before + 1
        preserved = [message(item) for item in observer.read()]
        assert preserved == [message(entry(1, b"session-write"))]
        released = trace.add("higher_controller_closed")
        higher.close()
        primary = driver.command("await-primary")
        assert primary["is_primary"] and primary["rpc_code"] == 0
        final_write = driver.command("modify")
        assert final_write["rpc_code"] == 0
        final = [message(item) for item in observer.read()]
        assert final == preserved
        return {"adapter": adapter, "repetition": repetition, "initial": initial, "backup": state,
                "backup_write": denied, "backup_write_rpcs": after - before, "primary": primary,
                "final_write": final_write, "final_entries": final,
                "writes": [row for row in trace.rows[offset:] if row["event"] == "write_end"],
                "takeover_sequence": takeover["sequence"], "demotion_sequence": demotion["sequence"],
                "backup_observed_sequence": observed["sequence"], "higher_closed_sequence": released["sequence"],
                "demotion_to_observation_ns": observed["monotonic_ns"] - demotion["monotonic_ns"]}
    finally:
        higher.close()
        observer.close()
        driver.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=__import__("pathlib").Path)
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    trace = Trace(args.output / "rpc.jsonl")
    target = Target(args.output / "target")
    relay = None
    rows = []
    try:
        target.start()
        relay = Relay(target.address, trace)
        with (args.output / "observations.jsonl").open("w") as file:
            for adapter in ("sdk", "finsy"):
                for repetition in range(1, args.repetitions + 1):
                    row = trial(adapter, repetition, relay, trace, args.output)
                    rows.append(row)
                    file.write(json.dumps(row) + "\n")
                    file.flush()
                    print(f"{adapter} mastership {repetition}/{args.repetitions}", flush=True)
    finally:
        if relay:
            relay.close()
        target.stop()
        trace.close()
        (args.output / "target-processes.json").write_text(json.dumps(target.history, indent=2) + "\n")
    snapshot(args.output, {"kind": "mastership", "trials": len(rows), "repetitions": args.repetitions})


if __name__ == "__main__":
    main()
